"""Guarded single-key save to flash (#559), against a faked device.

``urllib.request.urlopen`` is replaced by :class:`_FakeFlashDevice`, which
keeps two config images, RAM and flash, behind the routes the guard uses.
Each route behaves the way 1.1.0's ``route_configs.cc`` does:

* ``GET /v1/configs`` lists the categories, and ``GET /v1/configs/<cat>``
  returns the RAM values.  ``GET /v1/configs/<cat>/<item>`` returns the
  item map.
* ``PUT /v1/configs/<cat>/<item>?value=`` sets one RAM value.  An empty
  ``value=`` is the empty string (``ArgsURI::ParseReqHeader``).
* ``PUT /v1/configs/<cat>:load_from_flash`` copies flash into RAM for that
  category and answers ``{"loaded": [cat]}``.
* ``PUT /v1/configs/<cat>:save_to_flash`` copies RAM into flash when the
  store is dirty and answers ``{"written": [...]}``.

The global forms are recorded so a test can assert that they were never
sent.  No device traffic: there is no ``U64_HOST`` here and the host is
``h``.
"""
from __future__ import annotations

import json
import socket
import urllib.error
import urllib.parse
from io import BytesIO
from unittest.mock import patch

import pytest

import c64_test_harness as harness
from c64_test_harness.backends import ultimate64_temp_gc as gc_mod
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64ProtocolError,
)
from c64_test_harness.backends.ultimate64_flash import (
    FLASH_DIFF_SKIPPED,
    FlashSaveRefusedError,
    FlashSaveRestoreError,
    FlashSaveVerifyError,
    save_config_item_to_flash,
)

C64 = "C64 and Cartridge Settings"
FAST = "Fast Reset"
REU = "REU Size"
U64 = "U64 Specific Settings"
DRV = "Drive A Settings"
UI = "User Interface Settings"
NET = "Network Settings"
ETH = "Ethernet Settings"
SID = "SID Sockets Configuration"

EN_DIS = ["Disabled", "Enabled"]


def _enum(current, values=EN_DIS, default=None):
    return {"current": current, "values": list(values), "default": default or values[0]}


def _range(current, lo, hi, default):
    return {"current": current, "min": lo, "max": hi, "format": "%d", "default": default}


def _string(current, default=""):
    return {"current": current, "default": default}


def _items() -> dict:
    """RAM item maps, per category, as a C64U-shaped device lists them."""
    return {
        C64: {
            FAST: _enum("Disabled"),
            REU: _enum("512 KB", ["128 KB", "512 KB", "2 MB"], "2 MB"),
        },
        U64: {"CPU Speed": _enum(" 1", [" 1", " 2", "64"], " 1")},
        DRV: {"Drive Bus ID": _range(8, 8, 11, 8)},
        UI: {"Home Directory": _string("")},
        NET: {"FTP File Service": _enum("Enabled")},
        ETH: {"Use DHCP": _enum("Enabled")},
        SID: {"SID Socket 1": _enum("Enabled")},
    }


def _resp(obj) -> "_Resp":
    return _Resp(json.dumps(obj).encode())


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self) -> bytes:
        return self._body


class _FakeFlashDevice:
    """RAM and flash config images behind the config routes.

    :param ram: overrides of RAM values, ``{(cat, item): value}``.
    :param flash: overrides of flash values. Both images start from the
        same values, so an item named in only one of them differs.
    :param reject: ``(cat, item)`` whose PUT answers HTTP 400.
    :param save_noop: ``save_to_flash`` answers ``written`` but writes nothing.
    :param written_override: the ``written`` list ``save_to_flash`` answers.
    :param extra: additional categories, ``{cat: {item: item_map}}``.
    :param rewrite: ``{(cat, item): {written: stored}}`` -- a PUT of
        *written* stores *stored* instead (a hook that rewrites the item).
    :param put_hook: ``{(cat, item): ((cat2, item2), value)}`` -- a PUT of
        the first item also sets the second (a hook with a side effect).
    :param save_corrupt: ``{(cat, item): value}`` written into flash by
        ``save_to_flash`` on top of the RAM image.
    :param loaded_override: ``callable(cat, n)`` giving the ``loaded`` list
        for the *n*-th load of *cat* (0-based), or ``None`` for the default.
    """

    def __init__(self, *, ram=None, flash=None, reject=(), save_noop=False,
                 written_override=None, extra=None, rewrite=None, put_hook=None,
                 save_corrupt=None, loaded_override=None) -> None:
        self.items = _items()
        for cat, items in (extra or {}).items():
            self.items[cat] = {i: dict(m) for i, m in items.items()}
        # Flash starts equal to the unmodified RAM image; *ram* and *flash*
        # then move each side away from it.
        self.flash = {
            cat: {item: m["current"] for item, m in items.items()}
            for cat, items in self.items.items()
        }
        for (cat, item), value in (ram or {}).items():
            self.items[cat][item]["current"] = value
        for (cat, item), value in (flash or {}).items():
            self.flash[cat][item] = value
        self.reject = set(reject)
        self.rewrite = rewrite or {}
        self.put_hook = put_hook or {}
        self.save_corrupt = save_corrupt or {}
        self.loaded_override = loaded_override
        self.loads: dict[str, int] = {}
        self.save_noop = save_noop
        self.written_override = written_override
        self.dirty: set[str] = set()
        self.requests: list[tuple[str, str, dict, object]] = []

    # -- views used by assertions
    def ram(self) -> dict:
        return {cat: {i: m["current"] for i, m in items.items()}
                for cat, items in self.items.items()}

    def methods(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p, _q, _d in self.requests]

    def writes(self) -> list[tuple[str, str]]:
        return [(m, p) for m, p in self.methods() if m != "GET"]

    # -- the wire
    def __call__(self, req, timeout=None):
        split = urllib.parse.urlsplit(req.get_full_url())
        path = urllib.parse.unquote(split.path)
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        method = req.get_method()
        self.requests.append((method, path, query, req.data))
        assert req.data is None, "every request here is bodyless"
        parts = path.split("/")
        assert parts[:3] == ["", "v1", "configs"], path
        if method == "GET":
            if len(parts) == 3:
                return _resp({"categories": list(self.items), "errors": []})
            cat = parts[3]
            if len(parts) == 4:
                return _resp({cat: {i: m["current"] for i, m in self.items[cat].items()},
                              "errors": []})
            return _resp({cat: {parts[4]: dict(self.items[cat][parts[4]])}, "errors": []})
        assert method == "PUT"
        if len(parts) == 3:
            # The global :load_from_flash / :save_to_flash forms.
            return _resp({"errors": []})
        if len(parts) == 4:
            cat, _, verb = parts[3].rpartition(":")
            if verb == "load_from_flash":
                for item, value in self.flash[cat].items():
                    # An item only flash knows about appears in RAM too.
                    self.items[cat].setdefault(item, _string(value))["current"] = value
                self.dirty.discard(cat)
                n = self.loads.get(cat, 0)
                self.loads[cat] = n + 1
                loaded = [cat]
                if self.loaded_override is not None:
                    loaded = self.loaded_override(cat, n) or loaded
                return _resp({"loaded": loaded, "errors": []})
            if verb == "save_to_flash":
                written = [cat] if cat in self.dirty else []
                if written and not self.save_noop:
                    self.flash[cat] = {i: m["current"] for i, m in self.items[cat].items()}
                    for (c, i), v in self.save_corrupt.items():
                        if c == cat:
                            self.flash[c][i] = v
                self.dirty.discard(cat)
                if self.written_override is not None:
                    written = list(self.written_override)
                return _resp({"written": written, "errors": []})
            raise AssertionError(f"unexpected PUT {path}")
        cat, item = parts[3], parts[4]
        if (cat, item) in self.reject:
            raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, BytesIO(b"no"))
        value = query["value"][0]
        value = self.rewrite.get((cat, item), {}).get(value, value)
        m = self.items[cat][item]
        if "values" in m:
            match = [v for v in m["values"] if v.lower() == value.lower()]
            assert match, f"firmware would reject {value!r}"
            m["current"] = match[0]
        elif "min" in m:
            m["current"] = int(value, 0)
        else:
            m["current"] = value
        self.dirty.add(cat)
        if (cat, item) in self.put_hook:
            (c2, i2), v2 = self.put_hook[(cat, item)]
            self.items[c2][i2]["current"] = v2
        return _resp({"errors": []})


@pytest.fixture
def locked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gc_mod, "lock_held_for", lambda host: True)


@pytest.fixture
def unlocked(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(gc_mod, "lock_held_for", lambda host: False)


def _run(dev, *args, **kwargs):
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        return save_config_item_to_flash(c, *args, **kwargs)


def _global_routes(dev) -> list:
    return [p for _m, p in dev.writes() if p.split("/")[-1].startswith("configs:")]


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("dry_run", [False, True])
def test_refuses_without_the_device_lock_before_any_request(unlocked, dry_run):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, C64, FAST, "Disabled", dry_run=dry_run)
    assert dev.requests == []


@pytest.mark.parametrize("cat", sorted(FLASH_DIFF_SKIPPED))
def test_refuses_a_target_in_a_skipped_store_before_any_request(locked, cat):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, cat, "Anything", "x")
    assert dev.requests == []


@pytest.mark.parametrize("cat", [U64, "Audio Mixer", "SID Addressing", "UltiSID Configuration"])
def test_refuses_a_target_on_the_page_it_shares_with_sid_sockets(locked, cat):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, cat, "CPU Speed", "64")
    assert dev.requests == []


def test_refuses_a_glob_category(locked):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, "C64*", FAST, "Disabled")
    assert dev.requests == []


def test_refuses_an_invalid_value_with_reads_only(locked):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, C64, FAST, "Maybe")
    assert dev.writes() == []


def test_refuses_an_out_of_range_number_with_reads_only(locked):
    """The firmware answers 400 outside min..max (``set_item``); refuse first."""
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, DRV, "Drive Bus ID", 12)
    assert dev.writes() == []


def test_refuses_when_another_item_differs_and_leaves_ram_and_flash(locked):
    """The core guard: a second RAM-vs-flash difference blocks the save."""
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled", (DRV, "Drive Bus ID"): 9},
        flash={(C64, FAST): "Enabled"},
    )
    ram_before = dev.ram()
    flash_before = json.loads(json.dumps(dev.flash))
    with pytest.raises(FlashSaveRefusedError) as info:
        _run(dev, C64, FAST, "Disabled")
    report = info.value.report
    assert (DRV, "Drive Bus ID") in report.differences
    assert report.saved is False
    assert dev.ram() == ram_before
    assert dev.flash == flash_before
    assert not any(p.endswith(":save_to_flash") for _m, p in dev.writes())


def test_restores_ram_after_each_load_on_refusal(locked):
    """A differing item gets its RAM value back by a bodyless verified PUT."""
    dev = _FakeFlashDevice(
        ram={(C64, REU): "512 KB"}, flash={(C64, REU): "2 MB"},
    )
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, C64, FAST, "Enabled")
    assert dev.items[C64][REU]["current"] == "512 KB"
    put = [(p, q) for m, p, q, _d in dev.requests if m == "PUT" and p.endswith(f"/{REU}")]
    assert put and put[0][1]["value"] == ["512 KB"]


def test_restores_an_empty_string_with_an_empty_value(locked):
    """RAM "" vs flash "foo" is restored by ``?value=``, the empty string."""
    dev = _FakeFlashDevice(
        ram={(UI, "Home Directory"): ""}, flash={(UI, "Home Directory"): "/SD"},
    )
    with pytest.raises(FlashSaveRefusedError) as info:
        _run(dev, C64, FAST, "Enabled")
    assert dev.items[UI]["Home Directory"]["current"] == ""
    assert (UI, "Home Directory") in info.value.report.differences
    put = [q for m, p, q, _d in dev.requests if m == "PUT" and p.endswith("/Home Directory")]
    assert put and put[0]["value"] == [""]


def test_a_rejected_restore_raises_the_restore_error_and_never_saves(locked):
    dev = _FakeFlashDevice(
        ram={(C64, REU): "512 KB"}, flash={(C64, REU): "2 MB"},
        reject={(C64, REU)},
    )
    with pytest.raises(FlashSaveRestoreError) as info:
        _run(dev, C64, FAST, "Enabled")
    err = info.value
    assert not isinstance(err, FlashSaveRefusedError)
    assert err.category == C64
    assert REU in err.items
    assert not any(p.endswith(":save_to_flash") for _m, p in dev.writes())


# ------------------------------------------------------------------ the save
def test_saves_the_one_key_and_confirms_it_from_flash(locked):
    """The #559 case: a run persisted Fast Reset = Enabled; put it back."""
    dev = _FakeFlashDevice(ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"})
    report = _run(dev, C64, FAST, "Disabled")
    assert report.saved is True
    assert report.flash_compared is True
    assert report.flash_before == "Enabled"
    assert dev.flash[C64][FAST] == "Disabled"
    assert dev.items[C64][FAST]["current"] == "Disabled"
    assert report.written == [C64]
    assert _global_routes(dev) == []
    saves = [p for _m, p in dev.writes() if p.endswith(":save_to_flash")]
    assert saves == [f"/v1/configs/{C64}:save_to_flash"]


def test_the_target_differing_alone_is_allowed(locked):
    dev = _FakeFlashDevice(ram={(C64, FAST): "Disabled"}, flash={(C64, FAST): "Enabled"})
    report = _run(dev, C64, FAST, "Disabled")
    assert report.saved is True
    assert dev.flash[C64][FAST] == "Disabled"


def test_skipped_stores_are_never_loaded_and_do_not_refuse(locked):
    """Network/Ethernet/SID Sockets differ in RAM, but they are not compared."""
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled"},
        flash={(C64, FAST): "Enabled", (NET, "FTP File Service"): "Disabled",
               (ETH, "Use DHCP"): "Disabled", (SID, "SID Socket 1"): "Disabled"},
    )
    report = _run(dev, C64, FAST, "Disabled")
    assert report.saved is True
    loaded = [p for _m, p in dev.writes() if p.endswith(":load_from_flash")]
    for cat in (NET, ETH, SID):
        assert f"/v1/configs/{cat}:load_from_flash" not in loaded
        assert cat in report.skipped_categories
        assert cat not in report.compared_categories
    assert set(report.compared_categories) == {C64, U64, DRV, UI}
    # The never-loaded stores kept their RAM values.
    assert dev.items[NET]["FTP File Service"]["current"] == "Enabled"


def test_flash_already_holding_the_value_skips_the_save(locked):
    dev = _FakeFlashDevice(ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Disabled"})
    report = _run(dev, C64, FAST, "Disabled")
    assert report.saved is False
    assert dev.items[C64][FAST]["current"] == "Disabled"
    assert not any(p.endswith(":save_to_flash") for _m, p in dev.writes())


def test_a_save_that_did_not_reach_flash_fails_the_confirm(locked):
    dev = _FakeFlashDevice(ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
                           save_noop=True)
    with pytest.raises(FlashSaveVerifyError):
        _run(dev, C64, FAST, "Disabled")


def test_a_save_that_wrote_other_categories_fails(locked):
    dev = _FakeFlashDevice(ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
                           written_override=[C64, NET])
    with pytest.raises(FlashSaveVerifyError):
        _run(dev, C64, FAST, "Disabled")


# ------------------------------------------------------------------ dry run
def test_dry_run_sends_only_gets_and_states_it_compared_nothing(locked):
    dev = _FakeFlashDevice(ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"})
    ram_before = dev.ram()
    flash_before = json.loads(json.dumps(dev.flash))
    report = _run(dev, C64, FAST, "Disabled", dry_run=True)
    assert dev.writes() == []
    assert dev.ram() == ram_before
    assert dev.flash == flash_before
    assert report.dry_run is True
    assert report.flash_compared is False
    assert report.flash_compared_reason
    assert report.saved is False
    assert report.ram_before == "Enabled"
    planned = report.planned_requests
    assert f"PUT /v1/configs/{C64}:save_to_flash" in planned
    assert f"PUT /v1/configs/{DRV}:load_from_flash" in planned
    assert not any(NET in p for p in planned)


# ------------------------------------------------------------------ client
def test_client_save_to_flash_per_category_returns_written():
    dev = _FakeFlashDevice()
    dev.dirty.add(C64)
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        written = c.save_config_to_flash(C64)
    assert written == [C64]
    assert dev.methods() == [("PUT", f"/v1/configs/{C64}:save_to_flash")]


def test_client_load_from_flash_per_category_returns_loaded():
    dev = _FakeFlashDevice()
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", dev):
        loaded = c.load_config_from_flash(C64)
    assert loaded == [C64]


def test_exported_at_the_package_root():
    assert harness.save_config_item_to_flash is save_config_item_to_flash
    assert issubclass(harness.FlashSaveRefusedError, Ultimate64Error)


# ------------------------------------------------------- review round 1 (#559)
def _no_save(dev) -> bool:
    return not any(p.endswith(":save_to_flash") for _m, p in dev.writes())


class _TimeoutAfterLoad(_FakeFlashDevice):
    """The first GET after a load times out (reviewer probe)."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.loaded = False
        self.failed = False

    def __call__(self, req, timeout=None):
        if req.get_full_url().endswith(":load_from_flash"):
            self.loaded = True
        elif self.loaded and not self.failed and req.get_method() == "GET":
            self.failed = True
            raise socket.timeout("blip")
        return super().__call__(req, timeout)


def test_a_get_timeout_after_a_load_puts_ram_back(locked):
    dev = _TimeoutAfterLoad(
        ram={(C64, REU): "512 KB", (C64, FAST): "Enabled"},
        flash={(C64, REU): "2 MB", (C64, FAST): "Enabled"},
    )
    with pytest.raises(FlashSaveRestoreError) as info:
        _run(dev, C64, FAST, "Disabled")
    assert info.value.category == C64
    assert info.value.items == []
    assert dev.items[C64][REU]["current"] == "512 KB"
    assert dev.items[C64][FAST]["current"] == "Enabled"
    assert _no_save(dev)


class _LoadReplyLost(_FakeFlashDevice):
    """The load is applied, but its reply never arrives."""

    def __call__(self, req, timeout=None):
        resp = super().__call__(req, timeout)
        if req.get_full_url().endswith(":load_from_flash"):
            raise socket.timeout("reply lost")
        return resp


def test_a_lost_load_reply_puts_ram_back(locked):
    dev = _LoadReplyLost(ram={(C64, REU): "512 KB"}, flash={(C64, REU): "2 MB"})
    with pytest.raises(FlashSaveRestoreError):
        _run(dev, C64, FAST, "Enabled")
    assert dev.items[C64][REU]["current"] == "512 KB"
    assert _no_save(dev)


SID_CHIP_STORES = [
    "SID Socket 1: ARMSID", "SID Socket 2: PDsid", "SID Socket 1: SidKick",
    "SID Socket 2: SidKick Pico", "SID Socket 1: FPGASID",
    "SID Socket 2: SwinSID Ultimate", "ARMSID in Socket 1", "FPGASID in Socket 2",
    "SwinSID Ultimate in Socket 1",
]


@pytest.mark.parametrize("cat", SID_CHIP_STORES)
def test_refuses_a_per_socket_sid_chip_store_before_any_request(locked, cat):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, cat, "SID Type", "8580")
    assert dev.requests == []


def test_a_listed_sid_chip_store_is_skipped_not_loaded(locked):
    chip = "SID Socket 1: PDsid"
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
        extra={chip: {"SID Type": _enum("8580", ["6581", "8580"])}},
    )
    report = _run(dev, C64, FAST, "Disabled")
    assert chip in report.skipped_categories
    assert chip not in report.compared_categories
    assert f"/v1/configs/{chip}:load_from_flash" not in [p for _m, p in dev.writes()]


def test_refuses_an_item_glob_before_any_request(locked):
    dev = _FakeFlashDevice()
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, C64, "Fast*", "Disabled")
    assert dev.requests == []


def test_a_restore_hook_that_undoes_an_earlier_restore_is_caught(locked):
    """Restoring REU Size rewrites Fast Reset, which was put back first."""
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled", (C64, REU): "512 KB"},
        flash={(C64, FAST): "Disabled", (C64, REU): "2 MB"},
        put_hook={(C64, REU): ((C64, FAST), "Disabled")},
    )
    with pytest.raises(FlashSaveRestoreError) as info:
        _run(dev, DRV, "Drive Bus ID", 9)
    assert info.value.category == C64
    assert FAST in info.value.items
    assert _no_save(dev)


def test_a_load_that_reports_extra_categories_is_a_restore_error(locked):
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
        loaded_override=lambda cat, n: [DRV, "Drive B Settings"] if cat == DRV else None,
    )
    with pytest.raises(FlashSaveRestoreError):
        _run(dev, C64, FAST, "Disabled")
    assert _no_save(dev)


def test_a_target_write_that_reads_back_wrong_is_put_back_and_refused(locked):
    dev = _FakeFlashDevice(rewrite={(DRV, "Drive Bus ID"): {"10": "11"}})
    with pytest.raises(FlashSaveRefusedError):
        _run(dev, DRV, "Drive Bus ID", 10)
    assert dev.items[DRV]["Drive Bus ID"]["current"] == 8
    assert _no_save(dev)


def test_a_target_write_and_its_put_back_both_failing_is_a_restore_error(locked):
    dev = _FakeFlashDevice(rewrite={(DRV, "Drive Bus ID"): {"10": "11", "8": "9"}})
    with pytest.raises(FlashSaveRestoreError) as info:
        _run(dev, DRV, "Drive Bus ID", 10)
    assert info.value.items == ["Drive Bus ID"]
    assert _no_save(dev)


def test_an_item_only_flash_knows_about_is_a_restore_error(locked):
    dev = _FakeFlashDevice(flash={(C64, "Ghost"): "boo"})
    with pytest.raises(FlashSaveRestoreError) as info:
        _run(dev, DRV, "Drive Bus ID", 9)
    assert "Ghost" in info.value.items
    assert _no_save(dev)


def test_a_save_that_corrupts_another_item_fails_the_confirm(locked):
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
        save_corrupt={(C64, REU): "128 KB"},
    )
    with pytest.raises(FlashSaveVerifyError):
        _run(dev, C64, FAST, "Disabled")


def test_a_confirming_load_that_reports_extra_categories_fails(locked):
    dev = _FakeFlashDevice(
        ram={(C64, FAST): "Enabled"}, flash={(C64, FAST): "Enabled"},
        loaded_override=lambda cat, n: [C64, DRV] if (cat == C64 and n == 1) else None,
    )
    with pytest.raises(FlashSaveVerifyError):
        _run(dev, C64, FAST, "Disabled")


def test_nothing_to_do_sends_no_write_to_the_target(locked):
    """RAM, flash and the value agree: no set PUT, no save."""
    dev = _FakeFlashDevice()
    report = _run(dev, C64, FAST, "Disabled")
    assert report.saved is False
    assert not any(p.endswith(f"/{FAST}") for _m, p in dev.writes())
    assert _no_save(dev)


@pytest.mark.parametrize("body", [
    b'{"written": [], "errors": ["boom"]}',
    b'["not", "an", "object"]',
    b'{"written": "C64 and Cartridge Settings", "errors": []}',
])
def test_client_save_to_flash_rejects_a_malformed_answer(body):
    c = Ultimate64Client("h")
    with patch("urllib.request.urlopen", lambda req, timeout=None: _Resp(body)), \
            pytest.raises(Ultimate64ProtocolError):
        c.save_config_to_flash(C64)
