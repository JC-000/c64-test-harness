"""Live test for the tape helpers (issue #584) on an Ultimate-II+ / II+L.

**Never run on a device, and it cannot run on this project's bench.** Tape
hardware exists only in the Ultimate-II+ / II+L (and 1541 Ultimate-II)
builds. On an Ultimate 64, Ultimate 64 Elite or C64 Ultimate the ``rig``
fixture skips the whole module with the reason ``tape_require_supported``
gives; no test here passes on a device without a recorder. The harness has
also never been run against an Ultimate-II+, so a failure in the fixture
itself (``create_manager``, storage) is as likely as one in the tape code.
The helpers and their fake are built from the firmware source: read this
module's failures as findings about that reading, not as regressions.

What it does, on the first removable volume the device lists (never
``/Temp``, never ``/Flash``), under ``/<volume>/c64-test-harness-live/``:

1. reads the Tape task menu (nothing recording, nothing playing);
2. a dry run of a capture start and of "Start Tape": the items exist under
   the names the source gives and can be highlighted;
3. a capture: starts "Capture save to TAP" to a named file, runs the pulse
   stimulus on the C64, stops the capture and checks the file's size,
   header and pulse lengths;
4. playback: uploads a TAP built from a known BASIC program, types
   ``LOAD``, starts the tape, waits the load out, stops the tape and checks
   that the program arrived.

Env gates (all unset -> skips cleanly):

* ``TAPE_LIVE=1`` -- master switch for this module.
* ``U64_HOST``    -- the device.

It resets the C64 and writes RAM (allowed on ``U64_HOST`` alone, #333). It
writes **no configuration**: "Telnet Remote Menu" and "FTP File Service"
must already be enabled, and the test fails with the error that names the
setting when one is not. It creates two files and deletes them in teardown.
This module's own RAM writes (the stimulus, under 200 bytes, and the
cleared load area) go through the transport, which chunks to query-string
PUTs on a leak-prone grade; the TAP goes up over FTP. Whatever the manager
itself spends is covered by the harness's ``/Temp`` hygiene.

The C64 is a PAL machine for the timing asserted here; on an NTSC machine
the pulse lengths are the same (they are counted in CPU cycles) and only the
waits differ.
"""
from __future__ import annotations

import os
import time

import pytest

from c64_test_harness import create_manager
from c64_test_harness.backends.ultimate64_storage import (
    storage_delete_file,
    storage_mkdir,
    storage_put_file,
    storage_writable_volumes,
)
from c64_test_harness.backends.ultimate64_tape import (
    TapeCapture,
    TapeUnsupportedError,
    tape_capture_start,
    tape_capture_stop,
    tape_load_and_verify,
    tape_play,
    tape_require_supported,
    tape_status,
    tape_stop,
)
from c64_test_harness.screen import wait_for_text
from c64_test_harness.tape import (
    build_tap_from_prg,
    build_tape_pulse_stimulus,
    run_tape_stimulus,
    tape_test_program,
)
from live_fixture_teardown import attempt_steps, raise_teardown_failures

_LIVE = os.environ.get("TAPE_LIVE")
_HOST = os.environ.get("U64_HOST")

pytestmark = [
    pytest.mark.skipif(not _LIVE, reason="TAPE_LIVE not set"),
    pytest.mark.skipif(not _HOST, reason="U64_HOST not set"),
]

_DIR = "c64-test-harness-live"
_CAPTURE_NAME = "h584cap"
_TAP_NAME = "h584load.tap"
_PULSES = 3000
_TAP_VALUE = 0x30


def _ready(transport, timeout: float = 25.0) -> None:
    transport.reset()
    if wait_for_text(transport, "READY.", timeout=timeout, poll_interval=0.3,
                     verbose=False) is None:
        pytest.fail(f"machine did not reach READY. within {timeout}s")
    time.sleep(5.0)   # the U64 rewrites $0801 a few seconds after the banner


def _delete_capture_when_not_recording(capture: TapeCapture) -> None:
    """Delete the capture file only after a positive "not recording" reading.

    ``tape_status`` raises on a Tape menu it cannot read, and a file the
    recorder still holds open must not be deleted under it.
    """
    status = tape_status(_HOST)
    if status.recording:
        raise RuntimeError(
            f"{_HOST} still shows a capture running ({status.items}); "
            f"{capture.path} was left in place")
    storage_delete_file(_HOST, capture.path, missing_ok=True)


@pytest.fixture(scope="module")
def rig():
    """A locked device with tape hardware, its target, and the scratch directory.

    ``create_manager`` holds the ``DeviceLock``; the autouse guard holds it
    too (``allow_nested``). A product without a tape recorder (an Ultimate
    64, 64 Elite or C64 Ultimate) skips the module with the reason
    ``tape_require_supported`` gives. Teardown stops any capture or playback
    this module left running and deletes its two files, each step on its
    own; the capture is deleted only once the device reads "not recording".
    """
    assert _HOST is not None
    failures: list = []
    with create_manager(backend="u64", u64_hosts=_HOST, lock_timeout=600.0) as mgr:
        with mgr.instance() as tgt:
            vols = storage_writable_volumes(_HOST)
            if not vols:
                pytest.skip(f"{_HOST} has no usable removable storage volume")
            directory = f"/{vols[0]}/{_DIR}"
            capture = TapeCapture(directory, _CAPTURE_NAME,
                                  f"{directory}/{_CAPTURE_NAME}.tap", True)
            storage_mkdir(_HOST, directory, exist_ok=True)
            try:
                product = tape_require_supported(_HOST, directory)
            except TapeUnsupportedError as exc:
                pytest.skip(f"no tape hardware on this device: {exc}")
            try:
                for name in (f"{_CAPTURE_NAME}.tap", _TAP_NAME):
                    storage_delete_file(_HOST, f"{directory}/{name}", missing_ok=True)
                yield tgt, directory, product
            finally:
                failures = attempt_steps([
                    ("stop capture", lambda: tape_capture_stop(_HOST, capture, read_file=False)),
                    ("stop tape", lambda: tape_stop(_HOST)),
                    ("delete capture", lambda: _delete_capture_when_not_recording(capture)),
                    ("delete tap", lambda: storage_delete_file(
                        _HOST, f"{directory}/{_TAP_NAME}", missing_ok=True)),
                ])
    raise_teardown_failures("tape rig teardown", failures)


def test_tape_menu_is_idle_and_readable(rig):
    """Only meaningful because ``rig`` has already seen a capture item."""
    status = tape_status(_HOST)
    assert not status.recording, f"a capture is already running: {status.items}"
    assert not status.playing and not status.paused, f"a tape is loaded: {status.items}"


def test_capture_and_start_tape_items_exist_under_their_source_names(rig):
    tgt, directory, _ = rig
    cap = tape_capture_start(_HOST, directory, _CAPTURE_NAME, dry_run=True)
    assert cap.started is False
    assert not tape_status(_HOST).recording
    prg = tape_test_program()
    path = f"{directory}/{_TAP_NAME}"
    storage_put_file(_HOST, path, build_tap_from_prg(prg), overwrite=True)
    res = tape_play(_HOST, path, dry_run=True)
    assert "Start Tape" in res.menu, res.menu
    assert not tape_status(_HOST).playing


def test_capture_records_the_stimulus_pulses(rig):
    tgt, directory, _ = rig
    _ready(tgt.transport)
    stim = build_tape_pulse_stimulus(_PULSES, tap_value=_TAP_VALUE)
    cap = tape_capture_start(_HOST, directory, _CAPTURE_NAME)
    try:
        assert tape_status(_HOST).recording
        finished = run_tape_stimulus(tgt, stim)
    finally:
        res = tape_capture_stop(_HOST, cap)
    assert finished, "the stimulus did not report done"
    assert res.was_recording
    assert res.path.casefold() == cap.path.casefold()
    assert res.status == "ok", (res.status, res.size, res.declared_length, res.data_length)
    assert res.size == 20 + res.data_length
    exact = [p for p in res.tap.pulses() if p == stim.period_cycles]
    near = [p for p in res.tap.pulses() if abs(p - stim.period_cycles) <= 8]
    # One interval per rising edge after the first; the recorder adds an
    # interval before the first edge and one when it is stopped.
    assert len(near) >= _PULSES - 1, (len(near), len(exact), res.tap.pulses()[:8])
    assert len(res.tap.pulses()) <= _PULSES + 8


def test_a_known_program_loads_from_tape(rig):
    tgt, directory, _ = rig
    prg = tape_test_program()
    path = f"{directory}/{_TAP_NAME}"
    storage_put_file(_HOST, path, build_tap_from_prg(prg), overwrite=True)
    _ready(tgt.transport)
    res = tape_load_and_verify(tgt.transport, _HOST, path, prg)
    assert res.play.status is not None and res.play.status.playing
    assert res.check.end_pointer == res.check.expected_end, res.check
    assert res.check.content_diffs == 0, res.check
    assert res.loaded, res.check
    assert res.after is not None and not res.after.playing and not res.after.paused
