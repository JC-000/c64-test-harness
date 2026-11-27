"""Tape capture and playback through the Remote Menu (issue #584), offline.

Functional: the tape helpers are run against ``tests/remote_menu_fake.py``
with ``tape=True``. That part of the fake is built from the firmware source
(69a5f83eb), not from recorded device bytes: it shows what the driver does
with the menus as read, not that a device draws them this way.
``tests/test_tape_live.py`` is the device check.

Safety: every call refuses without the ``DeviceLock`` before any
connection; a capture to ``/Temp`` or ``/Flash`` is refused before any menu
key; an existing file is never overwritten; menu items are chosen by name
whatever their position; the ENTER that creates a file or starts a tape is
sent once; and the load check reads no C64 memory until the wait is over
(#573), with the cassette buffer reserved in the ``MemoryPolicy`` meanwhile.

No test here talks to a device or spawns VICE.
"""

from __future__ import annotations

import pytest

from c64_test_harness import tape as tp
from c64_test_harness.backends import ultimate64_tape as ut
from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_remote_menu import (
    RemoteMenuLockError,
    RemoteMenuNavigationError,
    RemoteMenuNotFoundError,
    RemoteMenuTiming,
    RemoteMenuUnexpectedScreenError,
    RemoteMenuVerifyError,
    Ultimate64RemoteMenuError,
)
from c64_test_harness.backends.ultimate64_storage import StorageEntry
from c64_test_harness.memory_policy import MemoryPolicy, MemoryPolicyError, MemoryRegion

import remote_menu_fake as rmf
from lock_shell_fake import FORGED_IDS, FORGED_KW, fake_lock_shell, forge
from remote_menu_fake import Entry, FakeUltimate

FAST = RemoteMenuTiming(
    connect_timeout=2.0,
    first_byte_timeout=0.4,
    idle_gap=0.03,
    max_drain=5.0,
    poll_interval=0.03,
    first_probe_delay=0.05,
)

HOST = "127.0.0.1"
_NAV_KEYS = {"UP", "DOWN", "RIGHT", "LEFT", "PAGEUP", "PAGEDOWN", "ENTER", "o", "F1", "F5"}
_TEXT = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")

CAPTURE = "Capture save to TAP"
FINISH = "Finish Rec. to TAP"


class _Storage:
    """``storage_list_dir`` / ``storage_get_file`` over the fake's files."""

    def __init__(self, fake: FakeUltimate) -> None:
        self.fake = fake
        self.calls: list[tuple[str, str]] = []

    def list_dir(self, host, path, **kw):
        self.calls.append(("list", path))
        key = path + "/"
        if key not in self.fake.tree:
            raise FileNotFoundError(path)
        return [StorageEntry(e.name, "dir" if e.kind == "DIR" else "file",
                             len(self.fake.files.get(key + e.name, b"")))
                for e in self.fake.tree[key]]

    def get_file(self, host, path, **kw):
        self.calls.append(("get", path))
        if path not in self.fake.files:
            raise FileNotFoundError(path)
        return self.fake.files[path]


@pytest.fixture
def held(tmp_path):
    lock = DeviceLock(HOST, lock_dir=tmp_path / "locks", heartbeat_interval=None)
    assert lock.acquire(timeout=5)
    yield lock
    lock.release()


@pytest.fixture
def make(monkeypatch):
    fakes = []

    def _make(**kw):
        kw.setdefault("tape", True)
        fake = FakeUltimate(**kw)
        host, port = fake.start()
        fakes.append(fake)
        menu = ut.Ultimate64TapeMenu(host, port=port, timing=FAST, connector=fake.connector)
        storage = _Storage(fake)
        monkeypatch.setattr(ut, "DEFAULT_VERIFY_TIMEOUT", 1.5)
        monkeypatch.setattr(ut, "storage_list_dir", storage.list_dir)
        monkeypatch.setattr(ut, "storage_get_file", storage.get_file)
        return fake, menu, storage

    yield _make
    for f in fakes:
        for s in f.sessions:
            assert set(s.keys) <= _NAV_KEYS | _TEXT, f"disallowed key sent: {s.keys}"
            assert b"\n" not in bytes(s.raw)
        f.stop()


def _enters(fake) -> int:
    return sum(s.keys.count("ENTER") for s in fake.sessions)


def _typed(fake) -> str:
    return "".join(k for s in fake.sessions for k in s.keys if len(k) == 1)


class _Transport:
    def __init__(self) -> None:
        self.mem = bytearray(0x10000)
        self.log: list[tuple] = []
        self.memory_policy = MemoryPolicy()

    def write_memory(self, addr, data, **kw):
        self.memory_policy.check_write(addr, len(data))
        self.log.append(("write", addr, len(data)))
        self.mem[addr:addr + len(data)] = bytes(data)

    def read_memory(self, addr, length):
        self.log.append(("read", addr, length))
        return bytes(self.mem[addr:addr + length])

    def inject_keys(self, codes):
        self.log.append(("keys", bytes(codes)))


def _put_tap(fake, prg=None, path="/USB1/", name="known.tap"):
    prg = prg or tp.tape_test_program()
    fake.files[path + name] = tp.build_tap_from_prg(prg)
    fake.tree[path].append(Entry(name, "TAP", "30K"))
    return prg, path + name


# ---------------------------------------------------------------------------
# The lock gate
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda m, t: ut.tape_status(HOST, menu=m),
    lambda m, t: ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=m),
    lambda m, t: ut.tape_capture_start(HOST, "/USB1/Backups", "cap", dry_run=True, menu=m),
    lambda m, t: ut.tape_capture_stop(
        HOST, ut.TapeCapture("/USB1/Backups", "cap", "/USB1/Backups/cap.tap", True), menu=m),
    lambda m, t: ut.tape_play(HOST, "/USB1/known.tap", menu=m),
    lambda m, t: ut.tape_stop(HOST, menu=m),
    lambda m, t: ut.tape_load_and_verify(t, HOST, "/USB1/known.tap", tp.tape_test_program(),
                                         menu=m, sleep=lambda s: None),
    lambda m, t: m.tape_status(),
    lambda m, t: m.start_tape("/USB1/known.tap"),
    lambda m, t: m.stop_tape(),
    lambda m, t: m.finish_capture(),
    lambda m, t: m.start_capture(["USB1", "Backups"], "cap"),
], ids=["status", "capture_start", "capture_dry", "capture_stop", "play", "stop", "load",
        "m.status", "m.start_tape", "m.stop_tape", "m.finish", "m.start_capture"])
def test_every_call_refuses_without_the_lock_before_touching_anything(make, call):
    fake, menu, storage = make()
    _put_tap(fake)
    t = _Transport()
    with pytest.raises(RemoteMenuLockError, match="DeviceLock"):
        call(menu, t)
    assert fake.connect_attempts == 0 and fake.sessions == []
    assert storage.calls == []
    assert t.log == []


# ---------------------------------------------------------------------------
# Capture targets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("directory", ["/Temp", "/temp", "/Temp/sub", "/Flash", "/FLASH/roms"])
def test_capture_to_temp_or_flash_is_refused_before_any_menu_action(make, held, directory):
    fake, menu, storage = make()
    with pytest.raises(ValueError, match="refusing to write"):
        ut.tape_capture_start(HOST, directory, "cap", menu=menu)
    assert fake.connect_attempts == 0 and storage.calls == []
    with pytest.raises(ValueError, match="refusing to write"):
        ut.tape_capture_stop(HOST, ut.TapeCapture(directory, "cap", "x", True), menu=menu)
    assert fake.connect_attempts == 0 and storage.calls == []


@pytest.mark.parametrize("directory,name", [
    ("/USB1/Backups", ""), ("/USB1/Backups", "a.b"), ("/USB1/Backups", "a b"),
    ("/USB1/Backups", "a/b"), ("/USB1/Backups", "x" * 19), ("/USB1/Backups", "café"),
    ("/USB1/Backups", "../x"), ("USB1/Backups", "cap"), ("/USB1/Backups/", "cap"),
    ("/USB1/../Temp", "cap"), ("/", "cap"), ("/USB1/Backups", None),
])
def test_bad_capture_targets_are_refused_before_any_connection(make, held, directory, name):
    fake, menu, storage = make()
    with pytest.raises(ValueError):
        ut.tape_capture_start(HOST, directory, name, menu=menu)
    assert fake.connect_attempts == 0 and storage.calls == []


def test_an_existing_file_is_never_overwritten(make, held):
    fake, menu, storage = make()
    fake.tree["/USB1/Backups/"].append(Entry("CAP.TAP", "TAP", "1K"))
    fake.files["/USB1/Backups/CAP.TAP"] = b"keep me"
    with pytest.raises(FileExistsError, match="CAP.TAP"):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert fake.connect_attempts == 0
    assert fake.files["/USB1/Backups/CAP.TAP"] == b"keep me"


def test_a_missing_directory_is_refused_before_the_menu(make, held):
    fake, menu, storage = make()
    with pytest.raises(FileNotFoundError):
        ut.tape_capture_start(HOST, "/USB1/nowhere", "cap", menu=menu)
    assert fake.connect_attempts == 0


def test_a_second_capture_is_refused_while_one_runs(make, held):
    fake, menu, storage = make()
    fake.recording = "/USB1/other.tap"
    with pytest.raises(Ultimate64RemoteMenuError, match="already running"):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert fake.all_commits() == [] and _enters(fake) == 0
    assert fake.recording == "/USB1/other.tap"


# ---------------------------------------------------------------------------
# Starting a capture
# ---------------------------------------------------------------------------

def test_capture_start_types_the_name_and_creates_the_file_in_that_directory(make, held):
    fake, menu, storage = make()
    cap = ut.tape_capture_start(HOST, "/USB1/Backups", "cap-1_A", menu=menu)
    assert cap == ut.TapeCapture("/USB1/Backups", "cap-1_A", "/USB1/Backups/cap-1_A.tap", True)
    assert fake.all_commits() == [(f"Tape/{CAPTURE}:cap-1_A.tap", "/USB1/Backups/")]
    assert fake.recording == "/USB1/Backups/cap-1_A.tap"
    assert _typed(fake) == "cap-1_A"
    # One ENTER opened the prompt, one ended the name; the status reads sent none.
    assert _enters(fake) == 2
    committing = [s for s in fake.sessions if s.commits]
    assert len(committing) == 1 and committing[0].keys[-1] == "ENTER"


@pytest.mark.parametrize("recorder_first", [False, True])
@pytest.mark.parametrize("playing", [False, True])
def test_the_capture_item_is_chosen_by_name_wherever_it_sits(make, held, recorder_first, playing):
    fake, menu, storage = make(tape_recorder_first=recorder_first)
    if playing:
        fake.playing = "/USB1/known.tap"
    items = [label for label, _ in fake.tape_actions("/USB1/Backups/")]
    ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert fake.all_commits() == [(f"Tape/{CAPTURE}:cap.tap", "/USB1/Backups/")]
    # The positions really differed between the parametrised cases.
    assert items.index(CAPTURE) == (1 if recorder_first or not playing else 2)
    assert fake.playing == ("/USB1/known.tap" if playing else None)


def test_capture_dry_run_highlights_the_item_and_sends_no_enter(make, held):
    fake, menu, storage = make()
    cap = ut.tape_capture_start(HOST, "/USB1/Backups", "cap", dry_run=True, menu=menu)
    assert cap.started is False
    assert _enters(fake) == 0 and _typed(fake) == ""
    assert fake.all_commits() == [] and fake.recording is None and fake.files == {}


def test_a_directory_that_is_not_writable_offers_no_capture(make, held):
    fake, menu, storage = make(readonly_paths=("/USB1/Backups/",))
    with pytest.raises(ut.TapeUnsupportedError, match="not writable"):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert _enters(fake) == 0 and fake.files == {}


def test_a_disabled_capture_item_is_not_entered(make, held):
    fake, menu, storage = make(readonly_paths=("/USB1/Backups/",))
    fake.playing = "/USB1/known.tap"       # keeps the Tape category listed
    with pytest.raises(RemoteMenuNavigationError) as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert err.value.enter_sent is False
    assert _enters(fake) == 0 and fake.all_commits() == [] and fake.files == {}


def test_a_dropped_character_stops_the_call_and_creates_nothing(make, held):
    fake, menu, storage = make(box_drops="p")
    with pytest.raises(RemoteMenuNavigationError, match="prompt shows 'ca'") as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "capture", menu=menu)
    assert err.value.enter_sent is False
    assert _typed(fake) == "cap"                     # stopped at the first mismatch
    assert _enters(fake) == 1                        # only the one that opened the prompt
    assert fake.all_commits() == [] and fake.files == {} and fake.recording is None


def test_a_file_error_popup_is_reported_and_nothing_records(make, held):
    fake, menu, storage = make(capture_error="DISK FULL")
    with pytest.raises(ut.TapeMenuRejectedError) as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert err.value.popup == "DISK FULL" and err.value.enter_sent is True
    assert fake.recording is None
    assert "o" not in [k for s in fake.sessions for k in s.keys]


def test_a_lost_commit_is_a_verify_error_and_is_never_resent(make, held):
    fake, menu, storage = make(commit_takes_effect=False)
    with pytest.raises(RemoteMenuVerifyError, match="Not resent") as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", verify_timeout=0.2, menu=menu)
    assert err.value.enter_sent is True
    assert len(fake.all_commits()) == 1 and _enters(fake) == 2
    assert _typed(fake) == "cap"


def test_a_category_that_runs_an_action_when_opened_stops_the_call(make, held):
    fake, menu, storage = make(tape_category_runs_capture=True)
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="file-name prompt"):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert _typed(fake) == "" and _enters(fake) == 0
    assert fake.all_commits() == [] and fake.files == {}


# ---------------------------------------------------------------------------
# Stopping a capture: the file's path, size and status
# ---------------------------------------------------------------------------

def _started(fake, menu, name="cap"):
    return ut.tape_capture_start(HOST, "/USB1/Backups", name, menu=menu)


def test_capture_stop_reports_path_size_and_status(make, held):
    fake, menu, storage = make(capture_data=bytes([0x30]) * 100)
    cap = _started(fake, menu)
    res = ut.tape_capture_stop(HOST, cap, menu=menu)
    assert res.path == "/USB1/Backups/cap.tap"
    assert (res.size, res.declared_length, res.data_length) == (120, 100, 100)
    assert res.status == "ok" and res.ok and res.was_recording
    assert res.popup == rmf.TAPE_STOPPED
    assert res.tap.pulses() == [0x30 * 8] * 100
    assert fake.recording is None
    assert fake.all_commits()[-1] == (f"Tape/{FINISH}", "tasks")
    # The expected popup was answered with exactly one Ok.
    assert [k for s in fake.sessions for k in s.keys].count("o") == 1


def test_a_length_field_left_at_zero_is_a_length_mismatch(make, held):
    fake, menu, storage = make(finish_writes_length=False)
    res = ut.tape_capture_stop(HOST, _started(fake, menu), menu=menu)
    assert res.status == "length-mismatch" and not res.ok
    assert (res.size, res.declared_length, res.data_length) == (84, 0, 64)


def test_a_capture_with_no_pulses_is_empty(make, held):
    fake, menu, storage = make(capture_data=b"")
    res = ut.tape_capture_stop(HOST, _started(fake, menu), menu=menu)
    assert (res.status, res.size, res.data_length) == ("empty", 20, 0)


def test_stop_without_a_running_capture_sends_nothing_and_reports_missing(make, held):
    fake, menu, storage = make()
    cap = ut.TapeCapture("/USB1/Backups", "cap", "/USB1/Backups/cap.tap", True)
    res = ut.tape_capture_stop(HOST, cap, menu=menu)
    assert (res.status, res.size, res.was_recording, res.popup) == ("missing", None, False, None)
    assert _enters(fake) == 0 and fake.all_commits() == []


def test_stop_with_only_a_tape_playing_sends_nothing(make, held):
    """The Tape menu is listed (a tape plays) but offers no Finish."""
    fake, menu, storage = make()
    fake.playing = "/USB1/known.tap"
    cap = ut.TapeCapture("/USB1/Backups", "cap", "/USB1/Backups/cap.tap", True)
    res = ut.tape_capture_stop(HOST, cap, menu=menu)
    assert (res.status, res.was_recording) == ("missing", False)
    assert _enters(fake) == 0 and fake.playing == "/USB1/known.tap"


def test_a_file_that_is_not_a_tap_is_bad_header(make, held):
    fake, menu, storage = make()
    cap = _started(fake, menu)
    fake.capture_data = b""
    res_path = "/USB1/Backups/cap.tap"
    ut.tape_capture_stop(HOST, cap, menu=menu)
    fake.files[res_path] = b"not a tape at all, but long enough"
    res = ut.tape_capture_stop(HOST, cap, menu=menu)
    assert (res.status, res.size, res.tap) == ("bad-header", 34, None)


def test_read_file_false_sizes_the_file_without_reading_it(make, held):
    fake, menu, storage = make()
    cap = _started(fake, menu)
    storage.calls.clear()
    res = ut.tape_capture_stop(HOST, cap, read_file=False, menu=menu)
    assert (res.status, res.size) == ("unread", 84)
    assert [c[0] for c in storage.calls] == ["list"]


@pytest.mark.parametrize("recorder_first", [False, True])
def test_finish_is_chosen_by_name_next_to_the_player_items(make, held, recorder_first):
    fake, menu, storage = make(tape_recorder_first=recorder_first)
    cap = _started(fake, menu)
    fake.playing = "/USB1/known.tap"
    res = ut.tape_capture_stop(HOST, cap, menu=menu)
    assert res.was_recording and fake.recording is None
    assert fake.all_commits()[-1] == (f"Tape/{FINISH}", "tasks")
    assert fake.playing == "/USB1/known.tap" and fake.tape_paused is False


def test_an_unexpected_popup_after_finish_is_returned_and_not_answered(make, held):
    fake, menu, storage = make(finish_popup="Error during tape capture.")
    res = ut.tape_capture_stop(HOST, _started(fake, menu), menu=menu)
    assert res.popup == "Error during tape capture."
    assert "o" not in [k for s in fake.sessions for k in s.keys]


def test_a_finish_that_does_not_take_is_a_verify_error_and_is_not_resent(make, held):
    fake, menu, storage = make()
    cap = _started(fake, menu)
    fake.commit_takes_effect = False
    before = _enters(fake)
    with pytest.raises(RemoteMenuVerifyError, match="Not resent") as err:
        ut.tape_capture_stop(HOST, cap, verify_timeout=0.2, menu=menu)
    assert err.value.enter_sent is True
    assert _enters(fake) == before + 1 and fake.recording is not None


def test_finish_without_a_popup_still_reports_the_file(make, held):
    fake, menu, storage = make(finish_popup=None)
    res = ut.tape_capture_stop(HOST, _started(fake, menu), settle_timeout=0.3, menu=menu)
    assert res.popup is None and res.status == "ok"


# ---------------------------------------------------------------------------
# Playback
# ---------------------------------------------------------------------------

def test_tape_play_chooses_start_tape_and_confirms_on_a_fresh_connection(make, held):
    fake, menu, storage = make()
    _, path = _put_tap(fake)
    res = ut.tape_play(HOST, path, menu=menu)
    assert fake.all_commits() == [("Start Tape", "/USB1/")]
    assert fake.playing == path
    assert res.enter_sent and res.status.playing and not res.status.paused
    assert "Start Tape" in res.menu and "Run Tape" in res.menu
    assert _enters(fake) == 2          # one opened the file's menu, one chose the action


@pytest.mark.parametrize("prefix,initial", [((), 0), (("Select",), 0), (("Select", "View"), 3),
                                            ((), 7)])
def test_start_tape_is_chosen_by_name_not_by_position(make, held, prefix, initial):
    fake, menu, storage = make(context_prefix=prefix, menu_initial=initial)
    _, path = _put_tap(fake)
    ut.tape_play(HOST, path, menu=menu)
    assert fake.all_commits() == [("Start Tape", "/USB1/")]


def test_a_refused_tape_is_reported_with_the_firmwares_popup(make, held):
    fake, menu, storage = make(tap_start="popup:TAP file: invalid signature.")
    _, path = _put_tap(fake)
    with pytest.raises(ut.TapeMenuRejectedError) as err:
        ut.tape_play(HOST, path, menu=menu)
    assert err.value.popup == "TAP file: invalid signature." and err.value.enter_sent
    assert fake.playing is None


def test_tape_play_dry_run_commits_nothing(make, held):
    fake, menu, storage = make()
    _, path = _put_tap(fake)
    res = ut.tape_play(HOST, path, dry_run=True, menu=menu)
    assert res.dry_run and not res.enter_sent and res.status is None
    assert fake.all_commits() == [] and fake.playing is None


def test_a_file_without_start_tape_is_not_played(make, held):
    fake, menu, storage = make()
    with pytest.raises(RemoteMenuNotFoundError, match="Start Tape"):
        ut.tape_play(HOST, "/USB1/wraptor.d64", menu=menu)
    assert fake.all_commits() == [] and fake.playing is None


def test_a_start_that_does_not_take_is_a_verify_error(make, held):
    fake, menu, storage = make(commit_takes_effect=False)
    _, path = _put_tap(fake)
    with pytest.raises(RemoteMenuVerifyError, match="Not resent"):
        ut.tape_play(HOST, path, verify_timeout=0.2, menu=menu)
    assert len(fake.all_commits()) == 1


@pytest.mark.parametrize("recorder_first", [False, True])
def test_tape_stop_pauses_then_stops_and_leaves_a_capture_alone(make, held, recorder_first):
    fake, menu, storage = make(tape_recorder_first=recorder_first)
    fake.playing = "/USB1/known.tap"
    fake.recording = "/USB1/Backups/cap.tap"
    status = ut.tape_stop(HOST, menu=menu)
    assert fake.all_commits() == [("Tape/Pause Tape Playback", "tasks"),
                                  ("Tape/Stop Tape Playback", "tasks")]
    assert fake.playing is None and fake.recording == "/USB1/Backups/cap.tap"
    assert not status.playing and not status.paused and status.recording


def test_tape_stop_on_a_paused_tape_only_stops(make, held):
    fake, menu, storage = make()
    fake.playing, fake.tape_paused = "/USB1/known.tap", True
    ut.tape_stop(HOST, menu=menu)
    assert fake.all_commits() == [("Tape/Stop Tape Playback", "tasks")]
    assert fake.playing is None


def test_tape_stop_with_nothing_loaded_sends_no_enter(make, held):
    fake, menu, storage = make()
    status = ut.tape_stop(HOST, menu=menu)
    assert status.items == () and _enters(fake) == 0 and fake.all_commits() == []


def test_tape_status_reads_the_three_states(make, held):
    fake, menu, storage = make()
    assert ut.tape_status(HOST, menu=menu).items == ()
    fake.recording = "/USB1/x.tap"
    st = ut.tape_status(HOST, menu=menu)
    assert (st.recording, st.playing, st.paused) == (True, False, False)
    fake.playing = "/USB1/known.tap"
    st = ut.tape_status(HOST, menu=menu)
    assert (st.recording, st.playing, st.paused) == (True, True, False)
    fake.tape_paused, fake.recording = True, None
    st = ut.tape_status(HOST, menu=menu)
    assert (st.recording, st.playing, st.paused) == (False, False, True)
    assert _enters(fake) == 0


def test_read_string_box_finds_the_prompt_only_when_it_is_drawn(make, held):
    fake, menu, storage = make()
    sess = menu._open("test")
    try:
        assert ut.read_string_box(sess.screen, ut.NAME_PROMPT) is None
        menu._enter_directory(sess, ["USB1", "Backups"])
        menu._open_tape(sess, "test")
        menu._highlight(sess, CAPTURE, "the Tape task menu")
        sess.press("ENTER")
        assert ut.read_string_box(sess.screen, ut.NAME_PROMPT) == ""
        assert ut.read_string_box(sess.screen, "Some other prompt") is None
        sess.type_char("Z")
        assert ut.read_string_box(sess.screen, ut.NAME_PROMPT) == "Z"
        with pytest.raises(Ultimate64RemoteMenuError, match="refusing to type"):
            sess.type_char(".")
        with pytest.raises(Ultimate64RemoteMenuError, match="refusing to type"):
            sess.type_char("\r")
    finally:
        sess.close()
    assert fake.files == {}          # the prompt was abandoned, not ended


# ---------------------------------------------------------------------------
# LOAD from tape, checked after the wait
# ---------------------------------------------------------------------------

def _loader(t, prg, *, status=0x00, on_sleep=None):
    """A ``sleep`` that stands for the C64 loading *prg* while the host waits."""
    def sleep(seconds):
        t.log.append(("sleep", seconds))
        if on_sleep is not None:
            on_sleep()
        if prg is not None:
            t.mem[prg.load_address:prg.load_address + len(prg.data)] = prg.data
            end = prg.load_address + len(prg.data)
            t.mem[0xAE], t.mem[0xAF] = end & 0xFF, end >> 8
            t.mem[0x90] = status
    return sleep


def test_load_and_verify_orders_everything_around_the_wait(make, held):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    t.mem[0x0801:0x0801 + len(prg.data)] = b"\xEE" * len(prg.data)   # stale junk
    seen = {}

    inject = t.inject_keys

    def inject_keys(codes):
        seen["playing_when_load_was_typed"] = fake.playing
        inject(codes)

    t.inject_keys = inject_keys

    def during():
        seen["playing"] = fake.playing
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0x0360, b"\x00")          # the jsr() trampoline address
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0x03C0, b"\x00")

    res = ut.tape_load_and_verify(t, HOST, path, prg, found_pause=3.0, margin=1.0,
                                  menu=menu, sleep=_loader(t, prg, on_sleep=during))
    assert res.loaded and res.check.complete
    kinds = [e[0] for e in t.log]
    keys_at, sleep_at = kinds.index("keys"), kinds.index("sleep")
    assert t.log[keys_at] == ("keys", b"LOAD\r")
    # Before the LOAD: the area and the end pointer were zeroed.
    writes = [e for e in t.log[:keys_at] if e[0] == "write"]
    assert sum(e[2] for e in writes if e[1] >= 0x0801) == len(prg.data)
    assert ("write", 0xAE, 2) in writes
    # #573: no read between the typed LOAD and the end of the wait.
    assert "read" not in kinds[:sleep_at]
    assert set(kinds[sleep_at + 1:]) == {"read"}
    # The tape was started after the LOAD was typed and was playing during the wait.
    assert seen["playing_when_load_was_typed"] is None
    assert seen["playing"] == path
    assert fake.all_commits() == [("Start Tape", "/USB1/"),
                                  ("Tape/Pause Tape Playback", "tasks"),
                                  ("Tape/Stop Tape Playback", "tasks")]
    assert fake.playing is None and res.after is not None and not res.after.playing
    tap = tp.parse_tap(fake.files[path])
    assert res.waited_seconds == pytest.approx(2.5 + tap.duration_seconds() + 3.0 + 1.0)
    assert t.log[sleep_at] == ("sleep", res.waited_seconds)
    # The guard is gone afterwards.
    t.write_memory(0x0360, b"\x00")


def test_a_load_that_never_ran_is_not_loaded(make, held):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    t.mem[0x0801:0x0801 + len(prg.data)] = prg.data      # an earlier copy, still in RAM
    end = 0x0801 + len(prg.data)
    t.mem[0xAE], t.mem[0xAF] = end & 0xFF, end >> 8
    res = ut.tape_load_and_verify(t, HOST, path, prg, found_pause=0, margin=0,
                                  menu=menu, sleep=_loader(t, None))
    assert not res.loaded
    assert res.check.end_pointer == 0 and res.check.content_diffs > 0


@pytest.mark.parametrize("status,loaded", [(0x00, True), (0x40, True), (0x10, False),
                                           (0x20, False), (0x04, False), (0x08, False)])
def test_a_tape_error_in_st_is_not_loaded_even_when_the_bytes_match(make, held, status, loaded):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    res = ut.tape_load_and_verify(t, HOST, path, prg, found_pause=0, margin=0,
                                  menu=menu, sleep=_loader(t, prg, status=status))
    assert res.check.complete and res.loaded is loaded


def test_a_policy_that_refuses_the_clearing_write_stops_before_load_is_typed(make, held):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    t.memory_policy = MemoryPolicy(reserved_regions=(MemoryRegion(0x0801, 0x0900, "program"),))
    original = t.memory_policy
    with pytest.raises(MemoryPolicyError):
        ut.tape_load_and_verify(t, HOST, path, prg, menu=menu, sleep=lambda s: None)
    assert t.log == []
    assert fake.all_commits() == [] and fake.playing is None
    assert t.memory_policy is original


def test_the_tape_is_stopped_even_when_the_wait_is_interrupted(make, held):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    original = t.memory_policy

    def sleep(seconds):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        ut.tape_load_and_verify(t, HOST, path, prg, menu=menu, sleep=sleep)
    assert fake.playing is None
    assert "read" not in [e[0] for e in t.log]
    assert t.memory_policy is original


def test_stop_after_false_leaves_the_tape_loaded(make, held):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    res = ut.tape_load_and_verify(t, HOST, path, prg, found_pause=0, margin=0, stop_after=False,
                                  menu=menu, sleep=_loader(t, prg))
    assert res.loaded and res.after is None and fake.playing == path


def test_a_file_that_is_not_a_tap_is_refused_before_anything_is_written(make, held):
    fake, menu, storage = make()
    fake.files["/USB1/wraptor.d64"] = b"\x00" * 64
    t = _Transport()
    with pytest.raises(ValueError, match="not a TAP"):
        ut.tape_load_and_verify(t, HOST, "/USB1/wraptor.d64", tp.tape_test_program(),
                                menu=menu, sleep=lambda s: None)
    assert t.log == [] and fake.connect_attempts == 0


# ---------------------------------------------------------------------------
# Round 2 (PR #589 review)
# ---------------------------------------------------------------------------

# -- a lock shell stands for the lock (#582) ---------------------------------

def test_a_verified_lock_shell_passes_the_tape_gate(make, monkeypatch):
    fake, menu, storage = make()
    fake.recording = "/USB1/x.tap"
    fake_lock_shell(monkeypatch, HOST)
    assert ut.tape_status(HOST, menu=menu).recording


@pytest.mark.parametrize("kw", FORGED_KW, ids=FORGED_IDS)
def test_a_forged_lock_shell_is_refused_before_any_connection(make, monkeypatch, kw):
    fake, menu, storage = make()
    forge(monkeypatch, HOST, kw)
    with pytest.raises(RemoteMenuLockError):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert fake.connect_attempts == 0 and storage.calls == []


# -- a device without tape hardware -------------------------------------------

def test_a_device_without_a_recorder_is_unsupported_not_idle(make, held):
    fake, menu, storage = make(tape=False)
    with pytest.raises(ut.TapeUnsupportedError, match="Ultimate 64 Elite") as err:
        ut.tape_require_supported(HOST, "/USB1/Backups", menu=menu)
    assert "Ultimate 64 Elite" in err.value.product
    assert _enters(fake) == 0 and _typed(fake) == ""
    with pytest.raises(ut.TapeUnsupportedError):
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert _enters(fake) == 0 and fake.files == {}


def test_a_device_with_a_recorder_is_supported(make, held):
    fake, menu, storage = make()
    assert "Ultimate 64 Elite" in ut.tape_require_supported(HOST, "/USB1/Backups", menu=menu)
    assert _enters(fake) == 0


def test_require_supported_refuses_temp_and_needs_the_lock(make, held):
    fake, menu, storage = make()
    with pytest.raises(ValueError):
        ut.tape_require_supported(HOST, "/Temp", menu=menu)
    assert fake.connect_attempts == 0
    held.release()
    with pytest.raises(RemoteMenuLockError):
        ut.tape_require_supported(HOST, "/USB1/Backups", menu=menu)
    assert fake.connect_attempts == 0
    assert held.acquire(timeout=5)


def test_a_tap_file_without_tape_actions_is_unsupported(make, held):
    fake, menu, storage = make(tape=False)
    _, path = _put_tap(fake)
    with pytest.raises(ut.TapeUnsupportedError, match="Ultimate 64 Elite"):
        ut.tape_play(HOST, path, menu=menu)
    assert fake.all_commits() == []


def test_load_and_verify_on_an_unsupported_device_types_nothing(make, held):
    fake, menu, storage = make(tape=False)
    prg, path = _put_tap(fake)
    t = _Transport()
    with pytest.raises(ut.TapeUnsupportedError):
        ut.tape_load_and_verify(t, HOST, path, prg, menu=menu, sleep=lambda s: None)
    assert t.log == [] and fake.all_commits() == []


# -- labels the driver does not know ------------------------------------------

def test_a_renamed_finish_item_raises_instead_of_reading_as_idle(make, held):
    fake, menu, storage = make(tape_renames={FINISH: "Finish Recording"})
    cap_path = "/USB1/Backups/cap.tap"
    fake.recording = cap_path
    fake.files[cap_path] = rmf.TAP_HEADER
    fake.tree["/USB1/Backups/"].append(Entry("cap.tap", "TAP", "0K"))
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="Finish Recording"):
        ut.tape_status(HOST, menu=menu)
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="Finish Recording"):
        ut.tape_capture_stop(HOST, ut.TapeCapture("/USB1/Backups", "cap", cap_path, True),
                             menu=menu)
    assert fake.recording == cap_path and _enters(fake) == 0


def test_an_unknown_extra_tape_item_stops_every_tape_call(make, held):
    fake, menu, storage = make(tape_extra_action="Rewind Tape")
    fake.playing = "/USB1/known.tap"
    for call in (lambda: ut.tape_status(HOST, menu=menu),
                 lambda: ut.tape_stop(HOST, menu=menu),
                 lambda: ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)):
        with pytest.raises(RemoteMenuUnexpectedScreenError, match="Rewind Tape"):
            call()
    assert _enters(fake) == 0 and fake.playing == "/USB1/known.tap"


def test_stop_raises_when_finish_is_not_offered_but_the_file_is_still_open(make, held):
    """No Finish item, yet the file's length field is still zero with data
    after it or nothing at all: the recorder writes the length when it closes."""
    fake, menu, storage = make()
    cap = ut.TapeCapture("/USB1/Backups", "cap", "/USB1/Backups/cap.tap", True)
    fake.tree["/USB1/Backups/"].append(Entry("cap.tap", "TAP", "0K"))
    fake.files[cap.path] = rmf.TAP_HEADER + bytes([0x30]) * 512
    with pytest.raises(ut.TapeCaptureNotStoppedError, match="cap.tap"):
        ut.tape_capture_stop(HOST, cap, menu=menu)
    fake.files[cap.path] = rmf.TAP_HEADER
    with pytest.raises(ut.TapeCaptureNotStoppedError):
        ut.tape_capture_stop(HOST, cap, menu=menu)
    assert _enters(fake) == 0


# -- the name prompt ------------------------------------------------------------

@pytest.mark.parametrize("mode,exc", [
    ("none", RemoteMenuUnexpectedScreenError),
    ("prefilled:old", RemoteMenuUnexpectedScreenError),
    ("popup:Some message", ut.TapeMenuRejectedError),
], ids=["no-prompt", "prefilled", "popup-instead"])
def test_nothing_is_typed_unless_an_empty_prompt_opened(make, held, mode, exc):
    fake, menu, storage = make(capture_prompt=mode)
    with pytest.raises(exc) as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert err.value.enter_sent is False
    assert _typed(fake) == ""
    assert _enters(fake) == 1                # the one that chose the item; none after it
    assert fake.files == {} and fake.recording is None
    assert "o" not in [k for s in fake.sessions for k in s.keys]


def test_require_supported_reads_a_running_capture_as_supported(make, held):
    fake, menu, storage = make()
    fake.recording = "/USB1/x.tap"
    assert ut.tape_require_supported(HOST, "/USB1/Backups", menu=menu)


def test_a_popup_over_the_prompt_stops_the_typing(make, held):
    fake, menu, storage = make(popup_after_chars=(1, "Some device message"))
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="Some device message") as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "foo", menu=menu)
    assert err.value.enter_sent is False
    assert _typed(fake) == "f"               # the next letter would have answered the popup
    assert _enters(fake) == 1 and fake.files == {}


def test_a_popup_before_the_committing_enter_stops_it(make, held):
    fake, menu, storage = make(popup_after_chars=(3, "Some device message"))
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="Some device message") as err:
        ut.tape_capture_start(HOST, "/USB1/Backups", "cap", menu=menu)
    assert err.value.enter_sent is False
    assert _enters(fake) == 1 and fake.files == {} and fake.recording is None


@pytest.mark.parametrize("name", ["Ok", "No", "Nomad", "Yes", "All", "Cancel", "Okay"])
def test_a_name_that_starts_like_a_button_is_typed_in_full(make, held, name):
    fake, menu, storage = make()
    ut.tape_capture_start(HOST, "/USB1/Backups", name, menu=menu)
    assert _typed(fake) == name
    assert fake.recording == f"/USB1/Backups/{name}.tap"


# -- tape_load_and_verify ---------------------------------------------------------

def test_a_start_that_cannot_be_confirmed_still_stops_the_tape(make, held, monkeypatch):
    """The ENTER was sent and the tape plays, but the confirming read fails."""
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    real = ut.Ultimate64TapeMenu._poll_status

    def poll(self, want, what, timeout):
        if what.startswith("Start Tape"):
            raise RemoteMenuVerifyError("slow menu", enter_sent=True)
        return real(self, want, what, timeout)

    monkeypatch.setattr(ut.Ultimate64TapeMenu, "_poll_status", poll)
    with pytest.raises(RemoteMenuVerifyError):
        ut.tape_load_and_verify(t, HOST, path, prg, menu=menu, sleep=lambda s: None)
    assert ("Start Tape", "/USB1/") in fake.all_commits()
    assert fake.playing is None                  # stopped on the way out
    assert "read" not in [e[0] for e in t.log]


@pytest.mark.parametrize("kw", [
    {"found_pause": -1.0}, {"margin": -0.1}, {"found_pause": float("nan")},
    {"margin": float("inf")}, {"clock_hz": 0}, {"clock_hz": -985248},
    {"clock_hz": float("nan")}, {"clock_hz": float("inf")}, {"found_pause": "3"},
])
def test_a_wait_that_could_end_early_is_refused_before_any_contact(make, held, kw):
    fake, menu, storage = make()
    prg, path = _put_tap(fake)
    t = _Transport()
    with pytest.raises(ValueError):
        ut.tape_load_and_verify(t, HOST, path, prg, menu=menu, sleep=lambda s: None, **kw)
    assert t.log == [] and storage.calls == [] and fake.connect_attempts == 0
