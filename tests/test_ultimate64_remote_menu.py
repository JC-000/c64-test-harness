"""The Telnet Remote Menu driver (issue #543), offline.

Functional: the VT100 model is checked against the bytes two devices sent
on 2026-11-09 and the screens the 1541ultimate lane's own renderer drew
from them; the driver is run against ``tests/remote_menu_fake.py``, a
telnet server that models the firmware's UI on 127.0.0.1.

Safety: every call refuses without the device's ``DeviceLock`` before
connecting; a state-changing ENTER is sent exactly once and verified on a
fresh connection, never resent; a dry run sends no committing key; Run
Update needs an explicit confirmation; only allow-listed keys reach the
wire. No test here talks to a device.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Error,
    Ultimate64UnsafeOperationError,
)
from c64_test_harness.backends import ultimate64_remote_menu as rm
from c64_test_harness.backends.ultimate64_remote_menu import (
    RemoteMenuConnectError,
    RemoteMenuLockError,
    RemoteMenuNotFoundError,
    RemoteMenuTiming,
    RemoteMenuUnexpectedScreenError,
    RemoteMenuVerifyError,
    TelnetScreen,
    Ultimate64RemoteMenu,
    Ultimate64RemoteMenuError,
    UpdateImageRejectedError,
    parse_wifi_row,
    read_view,
)

from remote_menu_fake import Entry, FakeUltimate

FIXTURES = Path(__file__).parent / "fixtures" / "remote_menu"

FAST = RemoteMenuTiming(
    connect_timeout=2.0,
    first_byte_timeout=0.5,
    idle_gap=0.03,
    max_drain=5.0,
    poll_interval=0.05,
)

#: Every key name the fake may ever see from the driver. Nothing that
#: deletes (ESC[2~ is KEY_INSERT = delete selected), no LF (0x0A is
#: KEY_CTRL_J = swap joysticks), no F-keys (their meaning differs by build).
ALLOWED_KEYS = {"UP", "DOWN", "RIGHT", "LEFT", "PAGEUP", "PAGEDOWN", "ENTER", "RUNSTOP", "o"}


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _rx_chunks(fixture: dict) -> list[bytes]:
    return [bytes.fromhex(e["hex"]) for e in fixture["events"] if e["dir"] == "RX"]


# ---------------------------------------------------------------------------
# The screen model, against real device bytes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "c64u_2026-11-09_flash_select.json",
    "u64e_2026-11-09_flash_select.json",
])
def test_replay_matches_the_lanes_rendering(name):
    """Feeding the recorded RX stream reproduces every screen the lane printed.

    The lane's recorder (1541u-tools ``flash_transcript.py``) dumps "screen
    after RX" from inside ``recv``, before its backend feeds that chunk, so
    such a screen shows every chunk but the one it is logged with. "screen
    after connect" is dumped after the connect drain and includes them all.
    """
    fx = _load(name)
    chunks = _rx_chunks(fx)
    assert fx["checkpoints"], "fixture has no screens to compare"
    for cp in fx["checkpoints"]:
        fed = cp["rx_chunks"] - (1 if cp["after"] == "RX" else 0)
        scr = TelnetScreen()
        for chunk in chunks[:fed]:
            scr.feed(chunk)
        ours = [r.rstrip() for r in scr.rows()]
        theirs = [r.rstrip() for r in cp["rows"]]
        assert ours == theirs, f"{name} @ {cp['t']} ({cp['after']})"


def test_screen_is_sixty_by_twenty_four_on_both_products():
    for name in ("c64u_2026-11-09_flash_select.json", "u64e_2026-11-09_flash_select.json"):
        chunks = _rx_chunks(_load(name))
        scr = TelnetScreen()
        for c in chunks[:3]:
            scr.feed(c)
        rows = scr.rows()
        assert len(rows) == 24 and all(len(r) == 60 for r in rows)
        assert rows[1] == "-" * 60  # the header rule spans exactly 60 columns


def test_negotiation_is_answered_and_never_drawn_even_split_across_reads():
    first = _rx_chunks(_load("c64u_2026-11-09_flash_select.json"))[0]
    assert first[:6] == b"\xff\xfe\x22\xff\xfb\x01"
    whole = TelnetScreen()
    replies_whole = whole.feed(first)
    split = TelnetScreen()
    replies_split = split.feed(first[:1]) + split.feed(first[1:4]) + split.feed(first[4:])
    # IAC WILL ECHO is accepted with IAC DO ECHO; IAC DONT LINEMODE needs no answer.
    assert replies_whole == [b"\xff\xfd\x01"]
    assert replies_split == [b"\xff\xfd\x01"]
    assert whole.rows() == split.rows()
    assert "\xff" not in "".join(split.rows())


def test_c64u_context_menu_is_parsed_from_the_recorded_bytes():
    fx = _load("c64u_2026-11-09_flash_select.json")
    chunks = _rx_chunks(fx)
    # Everything up to and including the RX that drew the menu (10:13:53.827).
    idx = [e["t"] for e in fx["events"] if e["dir"] == "RX"].index("10:13:53.827")
    scr = TelnetScreen()
    for c in chunks[: idx + 1]:
        scr.feed(c)
    view = read_view(scr)
    assert view.path == "/SD/"
    assert view.menu is not None
    assert view.menu.labels == ("Run Update", "Copy to...", "Move to...", "Rename", "Delete")
    assert view.menu.highlighted == 0
    assert view.popup is None


def test_u64e_listing_selection_is_read_from_colour():
    fx = _load("u64e_2026-11-09_flash_select.json")
    chunks = _rx_chunks(fx)
    idx = [e["t"] for e in fx["events"] if e["dir"] == "RX"].index("10:00:43.556")
    scr = TelnetScreen()
    for c in chunks[:idx]:   # before the menu opened
        scr.feed(c)
    view = read_view(scr)
    assert view.path == "/USB1/"
    assert view.menu is None
    assert view.selected is not None and view.selected.name == "vf_ue.u64"
    assert view.selected.kind == "U64"


@pytest.mark.parametrize("row,state,ident,ip,mac", [
    ("WiFi    ESP32 WiFi Bridge V0.903 bench             Disabled", "disabled",
     "ESP32 WiFi Bridge V0.903 bench", None, None),
    ("WiFi                                               Disabled", "disabled", None, None, None),
    ("WiFi    IP: 10.43.23.158                           Link Up", "link-up", None, "10.43.23.158", None),
    ("WiFi    MAC 3C:DC:75:4A:38:60                      Link Down", "link-down", None, None,
     "3C:DC:75:4A:38:60"),
    ("WiFi    ESP32 WiFi Bridge V0.903 bench             Scanning", "scanning", None, None, None),
    ("WiFi    Programming ESP32                          Busy", "busy", None, None, None),
    ("WiFi    ESP32                                      Firmware?", "firmware?", None, None, None),
    ("WiFi                                               No Module", "no-module", None, None, None),
])
def test_wifi_row_states(row, state, ident, ip, mac):
    st = parse_wifi_row(row)
    assert (st.state, st.ident, st.ip, st.mac) == (state, ident, ip, mac)


def test_wifi_row_rejects_other_rows_and_unknown_words():
    assert parse_wifi_row("Net0    IP: 10.43.23.26                            Link Up") is None
    with pytest.raises(RemoteMenuUnexpectedScreenError):
        parse_wifi_row("WiFi                                               Exploded")


def test_errors_are_ultimate64_errors():
    for cls in (Ultimate64RemoteMenuError, RemoteMenuLockError, RemoteMenuConnectError,
                RemoteMenuNotFoundError, RemoteMenuUnexpectedScreenError,
                RemoteMenuVerifyError, UpdateImageRejectedError):
        assert issubclass(cls, Ultimate64Error)


# ---------------------------------------------------------------------------
# The driver, against the fake server
# ---------------------------------------------------------------------------

@pytest.fixture
def lock_dir(tmp_path):
    return tmp_path / "locks"


@pytest.fixture
def held(lock_dir):
    lock = DeviceLock("127.0.0.1", lock_dir=lock_dir, heartbeat_interval=None)
    assert lock.acquire(timeout=5)
    yield lock
    lock.release()


@pytest.fixture
def make():
    fakes = []

    def _make(**kw):
        fake = FakeUltimate(**kw)
        host, port = fake.start()
        fakes.append(fake)
        menu = Ultimate64RemoteMenu(host, port=port, timing=FAST, connector=fake.connector)
        return fake, menu

    yield _make
    for f in fakes:
        for s in f.sessions:
            assert set(s.keys) <= ALLOWED_KEYS, f"disallowed key sent: {s.keys}"
            assert b"\n" not in bytes(s.raw)
        f.stop()


# -- the lock gate -----------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda m: m.wifi_status(),
    lambda m: m.set_wifi_enabled(False),
    lambda m: m.set_wifi_enabled(True, dry_run=True),
    lambda m: m.context_actions("/USB1/vf_ue.u64"),
    lambda m: m.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True),
    lambda m: m.run_update("/USB1/vf_ue.u64", dry_run=True),
    lambda m: m.read_screen(),
])
def test_every_call_refuses_without_the_lock_before_connecting(make, call):
    fake, menu = make()
    with pytest.raises(RemoteMenuLockError, match="DeviceLock"):
        call(menu)
    assert fake.connect_attempts == 0
    assert fake.sessions == []


def test_a_lock_on_another_device_does_not_count(make, lock_dir):
    fake, menu = make()
    other = DeviceLock("10.99.99.99", lock_dir=lock_dir, heartbeat_interval=None)
    assert other.acquire(timeout=5)
    try:
        with pytest.raises(RemoteMenuLockError):
            menu.wifi_status()
    finally:
        other.release()
    assert fake.connect_attempts == 0


# -- reading -------------------------------------------------------------------

def test_wifi_status_returns_the_ident_when_disabled(make, held):
    fake, menu = make(wifi={"state": "disabled", "ident": "ESP32 WiFi Bridge V1.494"})
    st = menu.wifi_status()
    assert st.state == "disabled"
    assert st.ident == "ESP32 WiFi Bridge V1.494"
    assert fake.all_commits() == []
    assert all(s.keys == [] for s in fake.sessions)  # the root row is read without a key


def test_read_screen_answers_negotiation_and_sends_no_keys(make, held):
    fake, menu = make()
    rows = menu.read_screen()
    assert rows[23].startswith("/ ")
    assert fake.sessions[0].iac_replies == [b"\xff\xfd\x01"]
    assert fake.sessions[0].keys == []


def test_context_actions_reads_the_menu_and_commits_nothing(make, held):
    fake, menu = make()
    labels = menu.context_actions("/USB1/vf_ue.u64")
    assert labels == ("Run Update", "Copy to...", "Move to...", "Rename", "Delete")
    assert fake.all_commits() == []


def test_navigation_enters_subdirectories(make, held):
    fake, menu = make()
    assert menu.context_actions("/USB1/firmware/cu_vf.ue2")[0] == "Run Update"
    assert fake.all_commits() == []


def test_missing_entry_is_not_found_and_commits_nothing(make, held):
    fake, menu = make()
    with pytest.raises(RemoteMenuNotFoundError, match="nope.u64"):
        menu.context_actions("/USB1/nope.u64")
    with pytest.raises(RemoteMenuNotFoundError, match="SD"):
        menu.context_actions("/SD/x.ue2")
    assert fake.all_commits() == []


def test_long_listing_is_paged(make, held):
    names = [f"file{i:03d}.prg" for i in range(70)]
    tree = {"/USB1/": [Entry(n, "PRG", "1K") for n in names] + [Entry("zz_last.u64", "U64", "5K")]}
    fake, menu = make(tree=tree)
    assert menu.context_actions("/USB1/zz_last.u64")[0] == "Run Update"
    keys = fake.sessions[-1].keys
    assert "PAGEDOWN" in keys
    assert keys.count("DOWN") < 40


def test_a_name_wider_than_the_name_field_is_refused_before_connecting(make, held):
    fake, menu = make()
    with pytest.raises(RemoteMenuNotFoundError, match="49"):
        menu.context_actions("/USB1/" + "x" * 49 + ".u64")
    assert fake.connect_attempts == 0


# -- modal screens on a fresh session --------------------------------------------

def test_sid_changed_message_is_dismissed_once_and_reported(make, held):
    fake, menu = make(messages=["SID changed. Please review settings"])
    st = menu.wifi_status()
    assert st.state == "link-up"
    assert fake.sessions[0].keys == ["o"]
    assert menu.dismissed_messages == ["SID changed. Please review settings"]


def test_an_unknown_message_stops_without_a_key(make, held):
    fake, menu = make(messages=["Recovery server not stored."])
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="Recovery server not stored"):
        menu.wifi_status()
    assert fake.sessions[0].keys == []


def test_password_prompt_is_refused(make, held):
    fake, menu = make(password="secret")
    with pytest.raises(RemoteMenuConnectError, match="[Pp]assword"):
        menu.wifi_status()
    assert bytes(fake.sessions[0].raw) == b""


def test_session_cap_is_reported(make, held):
    fake, menu = make(refuse_sessions=True)
    with pytest.raises(RemoteMenuConnectError, match="Too many connections"):
        menu.wifi_status()


# -- WiFi Disable / Enable -------------------------------------------------------

def test_disable_sends_one_enter_and_verifies_on_a_fresh_connection(make, held):
    fake, menu = make()
    res = menu.set_wifi_enabled(False)
    assert fake.all_commits() == [("Disable", "/")]
    assert res.enter_sent and not res.dry_run and res.changed
    assert res.before.state == "link-up"
    assert res.after.state == "disabled"
    assert res.after.ident == "ESP32 WiFi Bridge V0.903 bench"
    commit_session = next(s for s in fake.sessions if s.commits)
    # Nothing was sent on the committing session after its ENTER ...
    assert commit_session.keys[-1] == "ENTER"
    # ... and the verdict came from a later connection.
    assert len(fake.sessions) > commit_session.index + 1


def test_disable_succeeds_when_the_mirror_does_not_redraw(make, held):
    fake, menu = make(redraw_after_commit=False)
    res = menu.set_wifi_enabled(False)
    assert res.after.state == "disabled"
    assert fake.all_commits() == [("Disable", "/")]


def test_a_lost_enter_is_reported_not_resent(make, held):
    fake, menu = make(commit_takes_effect=False, redraw_after_commit=False)
    with pytest.raises(RemoteMenuVerifyError) as exc:
        menu.set_wifi_enabled(False, verify_timeout=0.5)
    assert exc.value.enter_sent is True
    assert fake.all_commits() == [("Disable", "/")]
    assert sum(s.keys.count("ENTER") for s in fake.sessions) == 2  # open menu + the one commit


def test_enable_waits_through_the_transitional_states(make, held):
    fake, menu = make(wifi={"state": "disabled", "ident": "ESP32 WiFi Bridge V0.903 bench"})
    res = menu.set_wifi_enabled(True)
    assert fake.all_commits() == [("Enable", "/")]
    assert res.before.ident == "ESP32 WiFi Bridge V0.903 bench"
    assert res.after.state in ("link-down", "link-up")


def test_already_in_the_target_state_opens_no_menu(make, held):
    fake, menu = make(wifi={"state": "disabled", "ident": "x"})
    res = menu.set_wifi_enabled(False)
    assert not res.enter_sent and not res.changed
    assert fake.all_commits() == []
    assert all("ENTER" not in s.keys for s in fake.sessions)


def test_transient_state_is_refused_without_a_key(make, held):
    fake, menu = make(wifi={"state": "scanning", "ident": "ESP32"})
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="scanning"):
        menu.set_wifi_enabled(False)
    assert all(s.keys == [] for s in fake.sessions)


def test_dry_run_opens_the_menu_but_commits_nothing(make, held):
    fake, menu = make()
    res = menu.set_wifi_enabled(False, dry_run=True)
    assert res.dry_run and not res.enter_sent
    assert res.menu == ("Forget APs", "Disconnect", "Disable")
    assert fake.all_commits() == []
    assert fake.wifi["state"] == "link-up"


def test_disabling_the_interface_we_are_connected_through_is_refused(make, held):
    fake, menu = make(wifi={"state": "link-up", "ip": "127.0.0.1"})
    with pytest.raises(RemoteMenuUnexpectedScreenError, match="connected through"):
        menu.set_wifi_enabled(False)
    assert fake.all_commits() == []


# -- Run Update ------------------------------------------------------------------

def test_run_update_needs_explicit_confirmation(make, held):
    fake, menu = make()
    with pytest.raises(Ultimate64UnsafeOperationError, match="confirm_device_powers_off"):
        menu.run_update("/USB1/vf_ue.u64")
    assert fake.connect_attempts == 0


def test_run_update_dry_run_commits_nothing(make, held):
    fake, menu = make()
    res = menu.run_update("/USB1/vf_ue.u64", dry_run=True)
    assert res.dry_run and not res.enter_sent and not res.started
    assert res.menu[0] == "Run Update"
    assert fake.all_commits() == []
    assert not fake.app_stopped


def test_run_update_sends_one_enter_and_confirms_the_application_stopped(make, held):
    fake, menu = make()
    res = menu.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True, verify_timeout=3.0)
    assert fake.all_commits() == [("Run Update", "/USB1/")]
    assert res.enter_sent and res.started
    assert any("no answer" in e for e in res.evidence)
    assert res.prompts_visible is False


def test_run_update_through_a_slow_load_does_not_resend(make, held):
    """The C64U case: the image takes seconds to load while new sessions still
    show the browser. The 2026-11-09 kit read that as a lost ENTER and pressed
    it again; the driver waits instead."""
    fake, menu = make(update_load_s=0.6)   # longer than FAST.first_byte_timeout
    res = menu.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True, verify_timeout=5.0)
    assert res.started
    assert fake.all_commits() == [("Run Update", "/USB1/")]
    assert len(fake.sessions) >= 2  # a fresh session answered during the load
    time.sleep(0.3)  # let the fake read anything still in flight
    committing = fake.sessions[0]
    # One CR opened the menu and one ran the action; nothing more went on the
    # wire, including while the application was busy loading the image.
    assert bytes(committing.raw).count(b"\r") == 2


def test_run_update_that_does_not_start_is_reported_not_resent(make, held):
    fake, menu = make(update="ignore")
    with pytest.raises(RemoteMenuVerifyError) as exc:
        menu.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True, verify_timeout=0.6)
    assert exc.value.enter_sent is True
    assert fake.all_commits() == [("Run Update", "/USB1/")]


def test_one_unanswered_connect_is_not_taken_as_started(make, held):
    """A single blip is not the application stopping: two in a row are needed."""
    fake, menu = make(update="ignore")
    calls = {"n": 0}

    def flaky(address, timeout=None):
        calls["n"] += 1
        if calls["n"] == 2:  # the first probe after the ENTER (call 1 opened the session)
            raise TimeoutError("blip")
        return fake.connector(address, timeout)

    menu._connector = flaky
    with pytest.raises(RemoteMenuVerifyError):
        menu.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True, verify_timeout=0.6)
    assert calls["n"] >= 3
    assert fake.all_commits() == [("Run Update", "/USB1/")]


def test_run_update_rejection_popup_is_reported_and_left_alone(make, held):
    fake, menu = make(update="reject:Update for Wrong Product")
    with pytest.raises(UpdateImageRejectedError, match="Update for Wrong Product") as exc:
        menu.run_update("/USB1/vf_ue.u64", confirm_device_powers_off=True, verify_timeout=2.0)
    assert exc.value.enter_sent is True
    assert fake.all_commits() == [("Run Update", "/USB1/")]
    assert "o" not in fake.sessions[0].keys


def test_run_update_on_a_file_without_the_action_commits_nothing(make, held):
    fake, menu = make()
    with pytest.raises(RemoteMenuNotFoundError, match="Run Update"):
        menu.run_update("/USB1/wraptor.d64", confirm_device_powers_off=True)
    assert fake.all_commits() == []


# -- the official C64U 1.1.0 layout (source-read, COMMODORE build) -----------------

def test_framed_layout_reads_and_navigates(make, held):
    fake, menu = make(
        framed=True,
        title="*** C64 Ultimate 1.1.0 *** Remote ***",
        root=[("SD", "SD Card", "Ready"), ("Flash", "Flash Disk", "Ready"),
              ("Temp", "RAM Disk", "Ready"), ("WiFi", "", "")],
        tree={"/SD/": [Entry("cu_vf.ue2", "UE2", "8830K")]},
        wifi={"state": "disabled", "ident": "ESP32 WiFi Bridge V1.11"},
    )
    st = menu.wifi_status()
    assert (st.state, st.ident) == ("disabled", "ESP32 WiFi Bridge V1.11")
    res = menu.run_update("/SD/cu_vf.ue2", dry_run=True)
    assert res.menu[0] == "Run Update"
    assert fake.all_commits() == []


def test_module_never_imports_a_rest_or_ftp_path():
    """Zero /Temp cost by construction: the driver has no REST or FTP call."""
    src = Path(rm.__file__).read_text()
    for forbidden in ("urllib", "http.client", "ftplib", "requests", "_request(", "Ultimate64Client("):
        assert forbidden not in src, forbidden
