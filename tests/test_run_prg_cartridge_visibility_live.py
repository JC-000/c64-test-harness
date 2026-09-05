"""Live U64 + external cartridge: which load paths leave the cartridge on the bus.

Issue #217 asked *which step* of ``Ultimate64Client.run_prg()`` leaves an
external cartridge (an RR-Net / CS8900a on the expansion port) deselected,
after #211 had only measured the outcome.  Measured on the U64E (fw 3.15
fork) with ``Cartridge Preference = External``, 2026-09-05, n=3 per arm,
interleaved, every arm started from a fresh re-PUT + reset:

============================================================  ===========
arm                                                           cartridge
============================================================  ===========
``run_prg_via_sys(prg)`` (REST write + typed SYS)             present 3/3
``client.run_prg(prg)``                                       absent  3/3
``client.load_prg(prg)`` alone, then a typed ``SYS``          absent  3/3
``transport.reset()`` alone, then ``run_prg_via_sys``         present 3/3
SocketDMA (TCP/64) write + typed ``SYS``                      present 2/2 [1]
after ``run_prg``: no reset                                   absent  3/3
after ``run_prg``: plain ``reset()``                          absent  3/3
after ``run_prg``: re-PUT ``Cartridge Preference``, no reset  present 3/3
after ``run_prg``: re-PUT, then ``reset()``                   present 3/3
============================================================  ===========

[1] the third SocketDMA trial's IDENTIFY barrier failed (connection
closed by peer) and no write was made, so it is not a sample.

So the mechanism is the firmware's **runner load path** (``/v1/runners:
load_prg`` and ``run_prg`` share it): it deselects the external cartridge
and the deselection is *sticky* -- it survives every ``reset()`` until the
``Cartridge Preference`` item is PUT again, even with the same value.
Neither the REST reset nor host DMA writes (REST or SocketDMA) touch it.

The sticky part matters more than the per-run part: one ``client.run_prg``
anywhere in a session leaves every later lane's cartridge invisible while
the config still reads ``External``.  ``run_prg_via_sys`` therefore re-PUTs
the preference before its reset on the U64 (``reselect_cartridge=True``),
which is what ``test_run_prg_via_sys_recovers_from_a_prior_run_prg``
pins.

The presence test is the only valid one on this bench: the 6510 reads
PacketPage ``$0000`` and gets ``$630E`` (issues #209/#211 -- raw ``$DE00``
bytes and host-side reads prove nothing).  The PRG under test *is* the
probe: a cc65 ``10 SYS2061`` stub whose ML enables the RR clockport, sets
PPPtr=$0000, copies PPData to ``$C1F0/$C1F1``, bumps a run counter at
``$C1F2`` and RTSes to BASIC.  Whatever started it, the 6510 reports the
answer and the host reads three bytes.

Gates (all unset -> the module skips cleanly):

* ``RRNET_LIVE=1`` -- master switch.
* ``U64_HOST``     -- the device (no IPs are committed).

Requirements: an RR-Net-compatible cartridge in the expansion port.  The
tests set ``Cartridge Preference = External`` and restore the original
value afterwards; config PUTs are volatile (nothing is saved to flash).
Never: ``poweroff``, ``reboot``, ``save_config_to_flash``.  No elevation
markers -- nothing here touches the host's network state.
"""

from __future__ import annotations

import os
import time

import pytest

from c64_test_harness import create_manager, run_prg_via_sys
from c64_test_harness.bridge_ping import (
    PPDATA_HI,
    PPDATA_LO,
    PPTR_HI,
    PPTR_LO,
    _clockport_enable_bytes,
)
from c64_test_harness.execute import load_code, run_subroutine
from c64_test_harness.memory import read_bytes, write_bytes
from c64_test_harness.screen import wait_for_text

_LIVE = os.environ.get("RRNET_LIVE")
_HOST = os.environ.get("U64_HOST")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="RRNET_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

CAT = "C64 and Cartridge Settings"
ITEM = "Cartridge Preference"

RESULT = 0xC1F0                  # PPData lo, PPData hi, run counter
SENTINEL = b"\xAA\xAA\x00"
PROBE_CODE = 0xC000              # run_subroutine home for the bare probe
IDENT = b"\x0e\x63"              # $630E little-endian: the CS8900a answered

CC65_STUB = bytes([0x01, 0x08, 0x0B, 0x08, 0x0A, 0x00, 0x9E]) + b"2061" + bytes(3)


def _probe_body() -> bytes:
    """Clockport on; PPPtr=$0000; PPData -> $C1F0/$C1F1; INC $C1F2; RTS."""
    return _clockport_enable_bytes() + bytes([
        0xA9, 0x00, 0x8D, PPTR_LO & 0xFF, PPTR_LO >> 8,
        0xA9, 0x00, 0x8D, PPTR_HI & 0xFF, PPTR_HI >> 8,
        0xAD, PPDATA_LO & 0xFF, PPDATA_LO >> 8, 0x8D, RESULT & 0xFF, RESULT >> 8,
        0xAD, PPDATA_HI & 0xFF, PPDATA_HI >> 8, 0x8D, (RESULT + 1) & 0xFF, RESULT >> 8,
        0xEE, (RESULT + 2) & 0xFF, RESULT >> 8,
        0x60,
    ])


PROBE_PRG = CC65_STUB + _probe_body()


def _wait_ready(transport, timeout: float = 25.0) -> None:
    if wait_for_text(transport, "READY.", timeout=timeout, poll_interval=0.3,
                     verbose=False) is None:
        pytest.fail(f"machine did not reach READY. within {timeout}s")


def _wait_for_probe(transport, timeout: float = 20.0) -> bytes:
    """Poll until the PRG's run counter moves, then return the 3 bytes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        raw = read_bytes(transport, RESULT, 3)
        if raw[2] != 0:
            return raw
        time.sleep(0.25)
    return read_bytes(transport, RESULT, 3)


def _identity(raw: bytes) -> str:
    if raw[:2] == IDENT:
        return "present"
    if raw[:2] == SENTINEL[:2]:
        return "probe never ran"
    return f"absent (PP $0000 read ${raw[1]:02X}{raw[0]:02X})"


def _probe_at_ready(target) -> bytes:
    """Presence read on a machine sitting at READY., no PRG involved."""
    t = target.transport
    write_bytes(t, RESULT, SENTINEL)
    load_code(t, PROBE_CODE, _probe_body())
    run_subroutine(target, PROBE_CODE, timeout=15.0, poll_cadence=0.005)
    return read_bytes(t, RESULT, 3)


def _fresh(target, settle: float = 1.0) -> None:
    """Re-PUT External, reset, READY., settle -- every test starts here."""
    t = target.transport
    t.client.set_config_item(CAT, ITEM, "External")
    time.sleep(0.5)
    t.reset()
    _wait_ready(t)
    time.sleep(settle)


@pytest.fixture(scope="module")
def target():
    """A locked U64 with ``Cartridge Preference = External`` for the module.

    The original preference is restored on the way out (volatile PUT, no
    flash write).  ``create_manager`` holds the ``DeviceLock``; the
    autouse ``device_lock_guard`` already holds it too (``allow_nested``).
    """
    with create_manager(backend="u64", u64_hosts=_HOST, lock_timeout=600.0) as mgr:
        with mgr.instance() as tgt:
            client = tgt.transport.client
            orig = client.get_config_category(CAT)[CAT][ITEM]
            try:
                _fresh(tgt)
                raw = _probe_at_ready(tgt)
                if raw[:2] != IDENT:
                    pytest.skip(
                        "no CS8900a answers on the 6510 with Cartridge Preference="
                        f"External ({_identity(raw)}); is an RR-Net cartridge fitted?"
                    )
                yield tgt
            finally:
                client.set_config_item(CAT, ITEM, orig)


def test_run_prg_via_sys_leaves_cartridge_selected(target):
    """The documented start path: RAM write + typed SYS keeps the cartridge."""
    t = target.transport
    _fresh(target)
    write_bytes(t, RESULT, SENTINEL)
    run_prg_via_sys(target, PROBE_PRG)
    raw = _wait_for_probe(t)
    assert raw[:2] == IDENT, f"run_prg_via_sys: cartridge {_identity(raw)}"


def test_run_prg_leaves_cartridge_deselected(target):
    """``client.run_prg`` deselects the cartridge -- the measured state.

    A plain assertion of the measured behaviour rather than an xfail: this
    is the reason ``run_prg_via_sys`` exists, and if a firmware build ever
    stops doing it the failure here is the signal to revisit the helper's
    rationale and every doc that cites #211/#217 -- an xfail that quietly
    XPASSes would bury exactly that.
    """
    t = target.transport
    _fresh(target)
    write_bytes(t, RESULT, SENTINEL)
    t.client.run_prg(PROBE_PRG)
    raw = _wait_for_probe(t)
    assert raw[2] != 0, "the runner never started the probe PRG"
    assert raw[:2] != IDENT, (
        "client.run_prg left the cartridge SELECTED -- the firmware runner "
        "path has changed; issues #211/#217 and run_prg_via_sys's docstring "
        "need revisiting"
    )


def test_load_prg_alone_deselects_cartridge(target):
    """The load half of the runner path is enough: no run, no reset."""
    t = target.transport
    _fresh(target)
    write_bytes(t, RESULT, SENTINEL)
    t.client.load_prg(PROBE_PRG)
    time.sleep(1.5)
    _wait_ready(t, timeout=10.0)
    head = read_bytes(t, 0x0801, 2)
    assert head == PROBE_PRG[2:4], f"load_prg did not put the PRG at $0801 ({head.hex()})"
    from c64_test_harness.keyboard import send_text
    send_text(t, "SYS2061\r")
    raw = _wait_for_probe(t)
    assert raw[2] != 0, "typed SYS never ran the probe"
    assert raw[:2] != IDENT, (
        "load_prg alone left the cartridge selected -- the #217 mechanism "
        "(runner load path deselects) no longer holds"
    )


def test_deselection_survives_reset_until_preference_is_re_put(target):
    """Sticky: reset() does not bring it back; re-PUTting the item does."""
    t = target.transport
    client = t.client
    _fresh(target)
    write_bytes(t, RESULT, SENTINEL)
    client.run_prg(PROBE_PRG)
    raw = _wait_for_probe(t)
    assert raw[:2] != IDENT, "precondition: run_prg did not deselect"

    t.reset()
    _wait_ready(t)
    time.sleep(1.5)
    after_reset = _probe_at_ready(target)
    assert after_reset[:2] != IDENT, (
        "a plain reset() re-selected the cartridge; #217 measured it sticky"
    )

    client.set_config_item(CAT, ITEM, "External")      # same value, PUT again
    time.sleep(0.5)
    t.reset()
    _wait_ready(t)
    time.sleep(1.5)
    after_put = _probe_at_ready(target)
    assert after_put[:2] == IDENT, (
        f"re-PUT of {ITEM} did not restore the cartridge: {_identity(after_put)}"
    )


def test_run_prg_via_sys_recovers_from_a_prior_run_prg(target):
    """Red without the re-PUT inside run_prg_via_sys (#217 harness fix).

    Sequence: run_prg (deselects, sticky) -> run_prg_via_sys with its own
    reset.  Measured before the fix: absent 3/3 (run 1, arm 3 after arm 1).
    """
    t = target.transport
    _fresh(target)
    write_bytes(t, RESULT, SENTINEL)
    t.client.run_prg(PROBE_PRG)
    assert _wait_for_probe(t)[:2] != IDENT, "precondition: run_prg did not deselect"

    write_bytes(t, RESULT, SENTINEL)
    run_prg_via_sys(target, PROBE_PRG)
    raw = _wait_for_probe(t)
    assert raw[:2] == IDENT, (
        "run_prg_via_sys after a client.run_prg: cartridge "
        f"{_identity(raw)} -- the helper did not re-select it"
    )
