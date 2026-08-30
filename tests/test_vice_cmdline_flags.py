"""``ViceConfig`` flag fields must set the VICE resource they claim to.

Every test here launches a real ``x64sc`` and reads the resource back
over the binary monitor.  That is deliberate: the fields these tests
cover were added in af479ee with a full set of unit tests that asserted
the flag text appeared in a mocked ``Popen`` argv, and *every one of the
flag names was wrong*.  VICE either rejected the flag outright or
prefix-matched it onto a different resource.  Asserting argv cannot
detect that; asserting the resource can.

The three failure modes those flags had (VICE 3.10 source):

* **Rejected as unknown.** ``-soundrecord`` / ``-recordfile`` do not
  exist; the real names are ``-soundrecdev`` / ``-soundrecarg``
  (S ``sound.c:806-812``).
* **Rejected as ambiguous.** ``-eventstart`` is a prefix of both
  ``-eventstartsnapshot`` and ``-eventstartmode`` (S ``event.c:1287,1293``),
  and ``lookup()`` (S ``cmdline.c:172-196``) reports ambiguity.
* **Silently bound to the wrong resource.** ``cmdline.c``'s ``lookup()``
  accepts any unambiguous *prefix*, so ``-eventsnapshot 1`` matched
  ``-eventsnapshotdir`` and set ``EventSnapshotDir="1"`` while VICE
  started up perfectly normally.

``-eventimage <path>`` combined two of them: it prefix-matched
``-eventimageinc``, a flag that takes no argument, so VICE set
``EventImageInclude`` to the value it already had by default
(S ``event.c:1248``) and then choked on the orphaned path — ``cmdline_parse``
breaks at an unconsumed argument (S ``cmdline.c:273``), leaving everything
after it unparsed, and VICE bailed with "Extra arguments on command-line".
"""

from __future__ import annotations

import socket

import pytest
from conftest import connect_binary_transport

from c64_test_harness.backends.vice_lifecycle import ViceConfig, ViceProcess

pytestmark = pytest.mark.vice_live


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def resources_after_launch(cfg: ViceConfig, *names: str) -> dict[str, int | str]:
    """Launch *cfg*, read *names* over the binary monitor, shut down.

    A flag VICE rejects makes the process exit before the monitor ever
    listens, so ``connect_binary_transport`` raises — which is exactly
    how the "rejected as unknown/ambiguous" defects surface here.
    """
    proc = ViceProcess(cfg)
    proc.start()
    try:
        transport = connect_binary_transport(cfg.port, proc=proc, timeout=25.0)
        try:
            return {name: transport.resource_get(name) for name in names}
        finally:
            transport.close()
    finally:
        proc.stop()


# ---------- sound recording ----------

def test_sound_record_driver_sets_sound_record_device_name():
    cfg = ViceConfig(port=free_port(), sound_record_driver="wav")
    got = resources_after_launch(cfg, "SoundRecordDeviceName")
    assert got["SoundRecordDeviceName"] == "wav"


def test_sound_record_file_sets_sound_record_device_arg(tmp_path):
    out = tmp_path / "capture.wav"
    cfg = ViceConfig(
        port=free_port(), sound_record_driver="wav", sound_record_file=str(out)
    )
    got = resources_after_launch(
        cfg, "SoundRecordDeviceName", "SoundRecordDeviceArg"
    )
    assert got["SoundRecordDeviceName"] == "wav"
    assert got["SoundRecordDeviceArg"] == str(out)


# ---------- event replay ----------

@pytest.mark.parametrize("mode", [0, 1, 2, 3])
def test_event_snapshot_mode_sets_event_start_mode(mode):
    """``EventStartMode`` accepts 0-3 (S ``event.c:1294``).

    Mode 3 (playback) also proves the validation range was widened: the
    old field capped at 2 and raised ``ValueError`` before VICE ever ran.
    """
    cfg = ViceConfig(port=free_port(), event_snapshot_mode=mode)
    got = resources_after_launch(cfg, "EventStartMode")
    assert got["EventStartMode"] == mode


def test_event_snapshot_dir_sets_event_snapshot_dir(tmp_path):
    """Control: this flag name was correct all along, and must stay so."""
    cfg = ViceConfig(port=free_port(), event_snapshot_dir=str(tmp_path))
    got = resources_after_launch(cfg, "EventSnapshotDir")
    # VICE normalises the resource to a trailing separator.
    assert str(got["EventSnapshotDir"]).rstrip("/") == str(tmp_path).rstrip("/")


def test_event_image_include_false_disables_the_resource():
    """The load-bearing direction: the factory default is already 1.

    ``EventImageInclude`` defaults to 1 (S ``event.c:1248``), so only the
    ``False`` case can distinguish a working flag from a no-op — which is
    precisely how the old ``-eventimage`` defect hid.
    """
    cfg = ViceConfig(port=free_port(), event_image_include=False)
    got = resources_after_launch(cfg, "EventImageInclude")
    assert got["EventImageInclude"] == 0


def test_event_image_include_true_enables_the_resource():
    cfg = ViceConfig(port=free_port(), event_image_include=True)
    got = resources_after_launch(cfg, "EventImageInclude")
    assert got["EventImageInclude"] == 1


def test_event_image_include_default_leaves_factory_value():
    cfg = ViceConfig(port=free_port())
    got = resources_after_launch(cfg, "EventImageInclude")
    assert got["EventImageInclude"] == 1


# ---------- fields with no VICE 3.10 equivalent ----------

@pytest.mark.parametrize("field", ["load_snapshot", "event_recording_start"])
def test_fields_without_a_vice_equivalent_are_gone(field):
    """Neither had any VICE 3.10 CLI equivalent, so both were removed.

    ``-loadsnapshot`` does not appear anywhere in the VICE 3.10 source;
    ``.vsf`` loading goes through the monitor's ``undump_snapshot()``.
    Event *recording* likewise has no CLI entry point at all — the whole
    ``-event*`` table is five options (S ``event.c:1279-1301``) and
    ``event_record_start()`` (S ``event.c:758``) is reachable only from
    the UI and the monitor.
    """
    assert not hasattr(ViceConfig(), field)


# ---------- sound forced on by a configured device ----------

def test_a_configured_sound_device_forces_sound_on(tmp_path):
    """``sounddev`` must enable ``Sound``, not merely name a device.

    The harness's comment has always said a configured device forces
    sound on, and for a long time the flag was simply not emitted.  It is
    emitted now, and this is what holds it there.

    The polarity is the whole test.  ``-sound`` and ``+sound`` are both
    valid VICE options, so the flag-name guard in
    ``test_vice_argv_contract.py`` passes either way -- a flipped
    polarity here is invisible to every other check in the suite, which
    is how it was measured surviving a mutation run.  Only reading the
    resource back distinguishes them.

    ``dummy`` is used deliberately: it exercises the same branch without
    opening a real audio device on the operator's machine.
    """
    cfg = ViceConfig(
        port=free_port(),
        sounddev="dummy",
        sound=False,  # the device must win over this
    )
    got = resources_after_launch(cfg, "Sound", "SoundDeviceName")
    assert got["Sound"] == 1, (
        "a configured sound device did not enable Sound; render_wav() and "
        "the SID suites would record silence"
    )
    assert got["SoundDeviceName"] == "dummy"
