"""``Ultimate64RemoteMenu.flash_esp`` and the ``tools.flash_esp`` CLI, against the fake."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import Ultimate64UnsafeOperationError
from c64_test_harness.backends.ultimate64_remote_menu import (
    EspFlashFailedError,
    EspFlashNoPopupError,
    EspFlashVerifyError,
    RemoteMenuLockError,
    RemoteMenuNotFoundError,
    RemoteMenuTiming,
    Ultimate64RemoteMenu,
)
from c64_test_harness.tools import flash_esp as cli

from remote_menu_fake import FakeUltimate

FAST = RemoteMenuTiming(connect_timeout=2.0, first_byte_timeout=0.5, idle_gap=0.03,
                        max_drain=5.0, poll_interval=0.05, first_probe_delay=0.05)
ALLOWED_KEYS = {"UP", "DOWN", "RIGHT", "LEFT", "PAGEUP", "PAGEDOWN", "ENTER", "o"}
ESP = "/Temp/raw_u64.esp"


@pytest.fixture
def held(tmp_path):
    lock = DeviceLock("127.0.0.1", lock_dir=tmp_path / "locks", heartbeat_interval=None)
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
        return fake, Ultimate64RemoteMenu(host, port=port, timing=FAST, connector=fake.connector)

    yield _make
    for f in fakes:
        for s in f.sessions:
            assert set(s.keys) <= ALLOWED_KEYS, f"disallowed key sent: {s.keys}"
            assert b"\n" not in bytes(s.raw)
        f.stop()


def _flash(menu, **kw):
    kw.setdefault("confirm_module_flash", True)
    kw.setdefault("ping", lambda ip: True)
    kw.setdefault("timeout", 3.0)
    kw.setdefault("verify_timeout", 3.0)
    return menu.flash_esp(ESP, **kw)


def _oks(fake):
    return sum(s.keys.count("o") for s in fake.sessions)


def test_success_answers_the_popup_once_and_verifies(make, held):
    fake, menu = make()
    pinged = []
    idents = iter(["OLD bridge", "ESP32 WiFi Bridge V1.2"])
    res = _flash(menu, expect_ident="ESP32 WiFi Bridge V1.2",
                 ident_probe=lambda: next(idents),
                 ping=lambda ip: pinged.append(ip) or True)
    assert res.ok and res.flash_ok and res.wifi_back and res.ping_ok and res.ident_ok is True
    assert res.popup == "Flashing ESP32 Success!"
    assert res.wifi_before.state == "link-up" and res.wifi_after.state == "link-up"
    assert res.enter_sent and res.duration_s > 0
    assert pinged == ["10.0.0.82"]
    assert fake.all_commits() == [("Flash into ESP32", "/Temp/")]
    assert _oks(fake) == 1 and fake.esp_restarts == 1
    # one CR opened the menu, one ran the action, on the committing session only
    committing = [x for x in fake.sessions if x.commits]
    assert len(committing) == 1 and bytes(committing[0].raw).count(b"\r") == 2
    assert committing[0].keys.count("o") == 1
    assert res.summary().startswith("OK flash_esp")


def test_error_popup_is_answered_once_reported_and_not_retried(make, held):
    fake, menu = make(esp="error:3")
    with pytest.raises(EspFlashFailedError) as exc:
        _flash(menu)
    assert exc.value.popup == "Flashing Error 3" and exc.value.enter_sent is True
    assert exc.value.module_restarted is True
    assert exc.value.result.flash_ok is False
    assert fake.all_commits() == [("Flash into ESP32", "/Temp/")]
    assert _oks(fake) == 1 and fake.esp_restarts == 1


def test_download_mode_failure_is_reported_as_not_restarted(make, held):
    fake, menu = make(esp="nodownload")
    with pytest.raises(EspFlashFailedError) as exc:
        _flash(menu)
    assert exc.value.module_restarted is False
    assert fake.esp_restarts == 0


def test_no_popup_is_loud_presses_nothing_and_does_not_retry(make, held):
    fake, menu = make(esp="none")
    with pytest.raises(EspFlashNoPopupError) as exc:
        _flash(menu, timeout=0.6)
    msg = str(exc.value)
    assert "nothing is retried" in msg and "does not leave it in download mode" in msg
    assert exc.value.enter_sent is True and exc.value.result.popup is None
    assert fake.all_commits() == [("Flash into ESP32", "/Temp/")]
    assert _oks(fake) == 0 and fake.esp_restarts == 0


def test_wifi_that_never_comes_back_fails_verification(make, held):
    fake, menu = make(wifi_after_esp=[{"state": "link-down", "mac": "3C:DC:75:4A:38:60"}])
    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, verify_timeout=0.5)
    r = exc.value.result
    assert r.flash_ok and not r.wifi_back and not r.ping_ok and not r.ok
    assert "Wi-Fi did not return" in str(exc.value)
    assert len(fake.all_commits()) == 1


def test_a_ping_that_never_answers_fails_verification(make, held):
    fake, menu = make()
    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, ping=lambda ip: False, verify_timeout=0.4)
    assert exc.value.result.wifi_back and not exc.value.result.ping_ok
    assert "no ping reply" in str(exc.value)


def test_ident_mismatch_fails_verification(make, held):
    fake, menu = make()
    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, expect_ident="ESP32 WiFi Bridge V2.0", ident_probe=lambda: "ESP32 WiFi Bridge V1.9")
    assert exc.value.result.ident_ok is False
    assert exc.value.result.ident_after == "ESP32 WiFi Bridge V1.9"
    assert "not the expected" in str(exc.value)


def test_an_unreadable_ident_cannot_pass_an_expectation(make, held):
    fake, menu = make()   # row is link-up: no ident shown, no probe given
    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, expect_ident="ESP32 WiFi Bridge V1.2")
    assert exc.value.result.ident_after is None and "not readable" in str(exc.value)


def test_ident_without_expectation_is_just_reported(make, held):
    fake, menu = make()
    res = _flash(menu, ident_probe=lambda: "whatever")
    assert res.ok and res.ident_ok is None and res.ident_after == "whatever"


def test_refuses_without_confirmation_before_connecting(make, held):
    fake, menu = make()
    with pytest.raises(Ultimate64UnsafeOperationError):
        menu.flash_esp(ESP)
    with pytest.raises(Ultimate64UnsafeOperationError):
        menu.flash_esp(ESP, confirm_module_flash=False)
    assert fake.connect_attempts == 0 and fake.sessions == []


def test_refuses_without_the_lock_before_connecting(make):
    fake, menu = make()
    with pytest.raises(RemoteMenuLockError):
        _flash(menu)
    assert fake.connect_attempts == 0


def test_refuses_when_the_target_is_the_wifi_address(make, held):
    fake, menu = make()
    with pytest.raises(Ultimate64UnsafeOperationError) as exc:
        _flash(menu, wifi_ip="127.0.0.1")
    assert "Wi-Fi address" in str(exc.value)
    assert fake.all_commits() == []
    # also when the row itself shows the host as the Wi-Fi IP
    fake2, menu2 = make(wifi={"state": "link-up", "ip": "127.0.0.1"})
    with pytest.raises(Ultimate64UnsafeOperationError):
        _flash(menu2)
    assert fake2.all_commits() == []


def test_missing_label_commits_nothing(make, held):
    fake, menu = make(esp_actions=False)   # a C64U build
    with pytest.raises(RemoteMenuNotFoundError) as exc:
        _flash(menu)
    assert "lacks the action" in str(exc.value)
    assert fake.all_commits() == [] and _oks(fake) == 0


def test_a_non_esp_path_is_refused_before_connecting(make, held):
    fake, menu = make()
    with pytest.raises(RemoteMenuNotFoundError):
        menu.flash_esp("/Temp/old.d64", confirm_module_flash=True)
    assert fake.sessions == []


# -- the CLI -------------------------------------------------------------------

class _Lock:
    def __init__(self, host):
        self.events = []

    def acquire_or_raise(self, timeout=None):
        self.events.append("acquire")

    def release(self):
        self.events.append("release")


def _run_cli(tmp_path, make, extra=(), dest="/USB1/espflash/raw_u64.esp", checker=None, readback=None, ident_probe=None, **fake_kw):
    fake, menu = make(**fake_kw)
    esp = tmp_path / "raw_u64.esp"
    esp.write_bytes(b"x")
    tool = tmp_path / "esp_image.py"
    tool.write_text("")
    calls = []
    lock = _Lock("h")
    rc = cli.main(
        ["--host", "127.0.0.1", "--esp", str(esp), "--platform", "u64e",
         "--esp-image-tool", str(tool), "--dest", dest, "--confirm-module-flash", "--timeout", "2",
         "--verify-timeout", "2", *extra],
        menu_factory=lambda host: menu, ping=lambda ip: True,
        put=lambda host, dest, path, **kw: calls.append(("put", dest)),
        get=lambda host, dest: b"x" if readback is None else readback,
        ident_probe=ident_probe, ident_poll=0.05,
        delete=lambda host, dest, **kw: calls.append(("delete", dest)),
        rmdir=lambda host, d, **kw: calls.append(("rmdir", d)),
        lock_factory=lambda host: lock,
        checker=checker or (lambda t, p, f: (True, f"OK {p}")))
    return rc, calls, lock, fake


def test_cli_success_prints_one_result_line(tmp_path, make, held, capsys):
    seq = iter(["OLD", "NEW"])
    rc, calls, lock, fake = _run_cli(tmp_path, make, ident_probe=lambda: next(seq))
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and out[-1].startswith("OK flash_esp /USB1/espflash/raw_u64.esp")
    assert calls == [("put", "/USB1/espflash/raw_u64.esp"), ("delete", "/USB1/espflash/raw_u64.esp"), ("rmdir", "/USB1/espflash")]
    assert lock.events == ["acquire", "release"]


def test_cli_a_failed_image_check_stops_before_any_device_access(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, checker=lambda t, p, f: (False, "bad chip"))
    assert rc == 2 and calls == [] and lock.events == [] and fake.sessions == []


def test_cli_failed_flash_exits_1_and_still_cleans_up(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, esp="error:5")
    assert rc == 1 and ("delete", "/USB1/espflash/raw_u64.esp") in calls
    assert lock.events == ["acquire", "release"]


def test_cli_refuses_the_wifi_ip_and_missing_confirmation(tmp_path, capsys):
    esp = tmp_path / "a.esp"
    esp.write_bytes(b"x")
    base = ["--host", "127.0.0.1", "--esp", str(esp), "--platform", "u64e",
            "--dest", "/USB1/espflash/a.esp"]
    assert cli.main(base + ["--wifi-ip", "127.0.0.1", "--confirm-module-flash"]) == 2
    assert cli.main(base) == 2


def test_cli_a_truncated_upload_is_refused_before_the_enter(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, readback=b"")
    assert rc == 2 and fake.all_commits() == [] and fake.sessions == []
    assert ("delete", "/USB1/espflash/raw_u64.esp") in calls


def test_cli_with_an_ident_probe_verifies_the_ident(tmp_path, make, held, capsys):
    seq = iter(["OLD bridge V1", "NEW bridge V2"])
    rc, calls, lock, fake = _run_cli(tmp_path, make, extra=["--expect-ident", "NEW bridge V2"],
                                     ident_probe=lambda: next(seq))
    assert rc == 0, capsys.readouterr()


def test_same_ident_as_before_is_refused_before_the_enter(make, held):
    fake, menu = make()
    with pytest.raises(Ultimate64UnsafeOperationError) as exc:
        _flash(menu, expect_ident="X bridge", ident_probe=lambda: "X bridge")
    assert "same-image" in str(exc.value) and fake.all_commits() == []
    res = _flash(menu, expect_ident="X bridge", ident_probe=lambda: "X bridge",
                 allow_same_ident=True)
    assert res.ok


@pytest.mark.parametrize("text", [
    "Flashing ESP32 Success!!", "Flashing ESP32 Success", "Flashing Error 3 extra",
    "Flashing Error", "Flashing Error 999", "flashing error 3", "Flashing Error 3\nmore"])
def test_only_an_exact_popup_text_is_answered(make, held, text):
    from c64_test_harness.backends.ultimate64_remote_menu import RemoteMenuUnexpectedScreenError
    fake, menu = make(esp=f"popup:{text}")
    with pytest.raises(RemoteMenuUnexpectedScreenError) as exc:
        _flash(menu)
    assert exc.value.enter_sent is True
    assert _oks(fake) == 0 and fake.esp_restarts == 0


def test_cli_reads_the_ident_only_outside_the_flash_window(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, extra=["--allow-same-ident"], ident_probe=None)
    labels = [c[0] for c in fake.all_commits()]
    assert labels == ["Disable", "Enable", "Flash into ESP32", "Disable", "Enable"], labels
    assert rc == 0


def test_cli_unchanged_ident_is_unproven_exit_3(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, ident_probe=lambda: "same")
    out = capsys.readouterr().out
    assert rc == 3 and "UNPROVEN flash_esp" in out


def test_ident_longer_than_31_chars_is_refused(make, held):
    fake, menu = make()
    with pytest.raises(Ultimate64UnsafeOperationError):
        _flash(menu, expect_ident="x" * 32, ident_probe=lambda: "y")
    assert fake.sessions == []


# -- review round 1 --------------------------------------------------------------

def test_an_explicit_wifi_ip_does_not_bypass_the_live_wifi_ip(make, held):
    """The device's own Wi-Fi row says 10.0.0.82; claiming another --wifi-ip must not
    let a session through that is on the live Wi-Fi address."""
    fake, menu = make(wifi={"state": "link-up", "ip": "127.0.0.1"})
    with pytest.raises(Ultimate64UnsafeOperationError):
        _flash(menu, wifi_ip="10.9.9.9")
    assert fake.all_commits() == []


def test_the_host_must_be_the_ethernet_address_the_device_reports(make, held):
    fake, menu = make(root=[("Temp", "RAM Disk", "Ready"), ("Net0", "IP: 10.1.1.1", "Link Up"),
                            ("WiFi", "", "")])
    with pytest.raises(Ultimate64UnsafeOperationError) as exc:
        _flash(menu)
    assert "Net0" in str(exc.value) and fake.all_commits() == []


def test_an_ident_probe_failure_after_the_flash_is_not_nothing_flashed(make, held):
    from c64_test_harness.backends.ultimate64_remote_menu import Ultimate64RemoteMenuError
    fake, menu = make()
    calls = iter([lambda: "OLD"])
    n = {"i": 0}

    def probe():
        n["i"] += 1
        if n["i"] == 1:
            return "OLD"
        raise Ultimate64RemoteMenuError("row unreadable", enter_sent=False)

    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, ident_probe=probe)
    assert exc.value.enter_sent is True and exc.value.result.flash_ok is True
    assert "ident" in str(exc.value)


def test_cli_failure_after_the_enter_exits_1_not_2(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, ident_probe=lambda: "x",
                                     esp="popup:Something unexpected")
    assert rc == 1 and ("delete", "/USB1/espflash/raw_u64.esp") in calls


def test_wifi_back_on_another_address_than_the_stated_one_fails(make, held):
    fake, menu = make()
    with pytest.raises(EspFlashVerifyError) as exc:
        _flash(menu, wifi_ip="10.0.0.99")
    assert "came back as" in str(exc.value)


def test_a_known_text_with_other_buttons_is_not_answered(make, held):
    from c64_test_harness.backends.ultimate64_remote_menu import RemoteMenuUnexpectedScreenError
    fake, menu = make(esp="yesno:Flashing ESP32 Success!")
    with pytest.raises(RemoteMenuUnexpectedScreenError):
        _flash(menu)
    assert _oks(fake) == 0


def test_cli_warns_not_to_touch_the_device(tmp_path, make, held, capsys):
    _run_cli(tmp_path, make, ident_probe=lambda: "x")
    assert "do not touch the U64E's keyboard or menu" in capsys.readouterr().out


@pytest.mark.parametrize("dest", ["/Temp/raw_u64.esp", "/temp/x/raw_u64.esp", "/Flash/raw_u64.esp",
                                  "/raw_u64.esp", "/USB1/espflash/raw_u64.bin"])
def test_cli_refuses_temp_flash_and_non_esp_destinations(tmp_path, make, held, capsys, dest):
    rc, calls, lock, fake = _run_cli(tmp_path, make, dest=dest)
    assert rc == 2 and calls == [] and fake.sessions == [] and lock.events == []
    assert "REFUSED" in capsys.readouterr().err


def test_cli_dest_is_required(tmp_path, capsys):
    with pytest.raises(SystemExit):
        cli.main(["--host", "h", "--esp", "a.esp", "--platform", "u64e"])


def test_cli_navigation_failure_is_a_clear_pre_enter_refusal(tmp_path, make, held, capsys):
    rc, calls, lock, fake = _run_cli(tmp_path, make, ident_probe=lambda: "x",
                                     dest="/USB1/nodir/raw_u64.esp")
    err = capsys.readouterr()
    assert rc == 2 and "REFUSED before the ENTER, nothing was flashed" in err.err
    assert "hint:" in err.err and "screen|" in err.err
    assert fake.all_commits() == []
    assert ("delete", "/USB1/nodir/raw_u64.esp") in calls   # the upload is still removed
