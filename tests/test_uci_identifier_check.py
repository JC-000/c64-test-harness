"""Every UCI routine fails fast when the UCI identifier is off the bus (#359).

Measured on the U64E (fw 3.15, paired ABBAAB, n=3 per arm): with
``Cartridge Preference`` = External the identifier at ``$DF1D`` was absent
after ``enable_uci`` + ``reset()`` + 3 s settle in 3/3 trials and every UCI
routine timed out at its sentinel (15 s in the live suites), while the
``Command Interface`` item still read Enabled.  With Auto it read ``$C9``
and every routine completed.

So ``_execute_uci_routine`` -- the one function every UCI routine goes
through -- now reads ``$DF1D`` first on an Ultimate transport and raises
``UCIInterfaceAbsentError`` (a ``UCIError``, never a timeout) before
anything is written or typed.  The message names ``Cartridge Preference``
and the Auto remedy, and includes the preference when one bodyless GET can
read it.  ``uci_probe`` opts out: returning the identifier is its contract.

Offline: a real ``Ultimate64Transport`` over a real ``Ultimate64Client``
whose ``_request`` is a recorder, so the wire shape (methods, bodies, the
``/Temp`` attachment count) is observed, not inferred.  No device, no VICE.
"""
from __future__ import annotations

import builtins
import json
from unittest.mock import MagicMock

import pytest

import c64_test_harness
from c64_test_harness import uci_network as u
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client, Ultimate64Error
from c64_test_harness.transport import TimeoutError as HarnessTimeout
from c64_test_harness.uci_network import (
    UCI_CMD_DATA_REG,
    UCI_IDENTIFIER,
    UCIError,
    UCIInterfaceAbsentError,
    _CODE_ADDR,
    _ERROR_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _execute_uci_routine,
)

_HOST = "198.51.100.7"  # TEST-NET-2; nothing is there, nothing is sent.
CODE = bytes([0xEA] * 20 + [0x60])
_PREF_PATH = "/v1/configs/C64%20and%20Cartridge%20Settings/Cartridge%20Preference"


class _Wire:
    """Records every request the client makes; answers like a U64 would."""

    def __init__(self, identifier: int, *, preference: str | None = "External",
                 preference_fails: bool = False) -> None:
        self.identifier = identifier
        self.preference = preference
        self.preference_fails = preference_fails
        self.calls: list[tuple[str, str, dict | None, bytes | None]] = []

    def __call__(self, method, path, *, body=None, content_type=None, query=None, **kw):
        self.calls.append((method, path, dict(query) if query else None, body))
        if path == "/v1/machine:readmem":
            addr = int(query["address"], 16)
            n = int(query["length"])
            if addr == UCI_CMD_DATA_REG:
                return 200, bytes([self.identifier]) * n
            if addr == _SENTINEL_ADDR:
                return 200, bytes([_SENTINEL_DONE]) * n
            return 200, bytes(n)
        if path.startswith("/v1/configs/"):
            if self.preference_fails:
                raise Ultimate64Error("config API busy", status=503)
            envelope = {"C64 and Cartridge Settings": {"Cartridge Preference": {
                "current": self.preference, "values": ["Auto", "Internal", "External", "Manual"],
                "default": "Auto"}}, "errors": []}
            return 200, json.dumps(envelope).encode()
        return 200, b""

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]

    def reads_of(self, addr: int) -> int:
        return sum(1 for m, p, q, _ in self.calls
                   if p == "/v1/machine:readmem" and int(q["address"], 16) == addr)


@pytest.fixture
def sleeps(monkeypatch):
    taken: list[float] = []
    monkeypatch.setattr(u.time, "sleep", taken.append)
    return taken


def _u64(wire: _Wire) -> Ultimate64Transport:
    client = Ultimate64Client(_HOST, write_mem_query_threshold=128,
                              warn_unlocked=False, temp_hygiene=False)
    client._request = wire  # type: ignore[method-assign]
    return Ultimate64Transport(host=_HOST, client=client)


# --------------------------------------------------------------------------- #
# Present: the routine runs as before, after one bodyless identifier read     #
# --------------------------------------------------------------------------- #

def test_identifier_present_runs_the_routine(sleeps):
    wire = _Wire(UCI_IDENTIFIER)
    t = _u64(wire)
    _execute_uci_routine(t, CODE, timeout=1.0)
    first = wire.calls[0]
    assert first[:2] == ("GET", "/v1/machine:readmem")
    assert first[2] == {"address": f"{UCI_CMD_DATA_REG:04X}", "length": "1"}
    assert wire.reads_of(UCI_CMD_DATA_REG) == 1
    # ...and then the routine really was uploaded and dispatched.
    written = [q["address"] for m, p, q, _ in wire.calls if p == "/v1/machine:writemem"]
    assert f"{_CODE_ADDR:04X}" in written and "0277" in written
    assert not any(p.startswith("/v1/configs") for _, p, _, _ in wire.calls)


# --------------------------------------------------------------------------- #
# Absent: refused before any write, clearly, at zero /Temp cost              #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("identifier", [0x00, 0xFF, 0xC8])
def test_identifier_absent_raises_before_any_write_or_sleep(sleeps, identifier):
    wire = _Wire(identifier)
    t = _u64(wire)
    with pytest.raises(UCIInterfaceAbsentError) as info:
        _execute_uci_routine(t, CODE, timeout=15.0)
    exc = info.value
    assert isinstance(exc, UCIError)
    assert not isinstance(exc, (HarnessTimeout, builtins.TimeoutError))
    assert exc.identifier == identifier
    assert exc.cartridge_preference == "External"
    msg = str(exc)
    assert f"${identifier:02X}" in msg and "$C9" in msg and "$DF1D" in msg
    # The cause clause itself, not just the remedy's config call (U9 survived
    # a looser "Cartridge Preference" in msg, which the remedy also matched).
    assert "Likely cause: Cartridge Preference = External (currently reads 'External')" in msg
    assert "or an external cartridge holding the bus" in msg
    assert "'Cartridge Preference', 'Auto'" in msg
    # Nothing written, nothing typed, no sentinel wait, no abort sleep.
    assert wire.methods() == ["GET", "GET"]
    assert [p for _, p, _, _ in wire.calls] == ["/v1/machine:readmem", _PREF_PATH]
    assert all(body is None for *_, body in wire.calls)
    assert t.client.pending_temp_attachments == 0
    assert sleeps == []


def test_a_failing_preference_read_still_raises_the_diagnosis(sleeps):
    wire = _Wire(0x00, preference_fails=True)
    with pytest.raises(UCIInterfaceAbsentError) as info:
        _execute_uci_routine(_u64(wire), CODE)
    assert info.value.cartridge_preference is None
    assert "Cartridge Preference" in str(info.value) and "could not be read" in str(info.value)
    assert wire.methods() == ["GET", "GET"]


@pytest.mark.parametrize("helper", ["uci_get_ip", "uci_status_peek", "uci_get_interface_count"])
def test_public_helpers_get_the_same_diagnosis(sleeps, helper):
    wire = _Wire(0x00)
    with pytest.raises(UCIInterfaceAbsentError):
        getattr(u, helper)(_u64(wire))
    assert "PUT" not in wire.methods() and "POST" not in wire.methods()


def test_uci_probe_opts_out_and_still_reports_the_identifier(sleeps):
    """``uci_probe`` returns what its routine read; the pre-check would turn
    its documented 0x00 into an exception."""
    wire = _Wire(0x00)
    assert u.uci_probe(_u64(wire)) == 0x00
    assert wire.reads_of(UCI_CMD_DATA_REG) == 0


def test_check_identifier_false_skips_the_read(sleeps):
    wire = _Wire(0x00)
    _execute_uci_routine(_u64(wire), CODE, timeout=1.0, check_identifier=False)
    assert wire.reads_of(UCI_CMD_DATA_REG) == 0


# --------------------------------------------------------------------------- #
# Other transports: unchanged                                                 #
# --------------------------------------------------------------------------- #

def test_a_non_ultimate_transport_is_not_checked(sleeps):
    """VICE and fakes: no ``$DF1D`` read, the routine runs as before."""
    t = MagicMock()
    reads: list[tuple[int, int]] = []

    def _read(addr, length):
        reads.append((addr, length))
        if addr == _SENTINEL_ADDR:
            return bytes([_SENTINEL_DONE])
        if addr == UCI_CMD_DATA_REG:
            return bytes([0x00])
        return bytes(length)

    t.read_memory.side_effect = _read
    _execute_uci_routine(t, CODE, timeout=1.0)
    assert (UCI_CMD_DATA_REG, 1) not in reads
    assert t.write_memory.call_count >= 3


def test_the_exception_is_exported_at_the_package_root():
    assert c64_test_harness.UCIInterfaceAbsentError is UCIInterfaceAbsentError
    assert "UCIInterfaceAbsentError" in c64_test_harness.__all__


def test_the_error_flag_path_is_unchanged(sleeps):
    """Control: a present identifier with the routine's error flag set is
    still a plain UCIError, not the new diagnosis."""
    wire = _Wire(UCI_IDENTIFIER)

    def _with_error(method, path, **kw):
        if path == "/v1/machine:readmem" and int(kw["query"]["address"], 16) == _ERROR_ADDR:
            wire.calls.append((method, path, dict(kw["query"]), None))
            return 200, b"\xff"
        return wire(method, path, **kw)

    t = _u64(wire)
    t.client._request = _with_error  # type: ignore[method-assign]
    with pytest.raises(UCIError) as info:
        _execute_uci_routine(t, CODE, timeout=1.0)
    assert not isinstance(info.value, UCIInterfaceAbsentError)
