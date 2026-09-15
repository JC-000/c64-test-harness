"""The harness never sends a malformed ``address`` to ``machine:writemem`` (#251).

Firmware without GideonZ/1541ultimate#884 parses ``address`` with a bare
``strtol(..., 16)``: unparseable input becomes ``0``, passes the range check,
and the write lands at ``$0000`` with **HTTP 200** (measured on the C64U,
fw 1.1.0, 2026-09-10).  The device will not help, so the harness guard is the
only protection until the C64U runs a release carrying #884.  #251 stays open
for that; these tests pin the guard.

What is pinned:

* ``write_mem`` / ``read_mem`` refuse a malformed address locally, with no
  request on the wire;
* ``_wire_hex16`` -- the single formatting choke point -- emits exactly four
  uppercase hex digits that round-trip, for every 16-bit address, and refuses
  everything else;
* every writemem request that reaches the wire, PUT or POST, carries such an
  address equal to the one asked for -- including ``$0000`` itself, the
  positive control that the strictness does not reject the real zero page;
* the paths that bypass ``write_mem`` (``liveness_probe`` and its restore)
  send the same strict form;
* no module in ``src`` builds an ``address`` query argument without
  ``_wire_hex16``, so a future hand-built query fails here first.

These pass against the code as it was: the guard predates the issue.  The
evidence that they can fail is the mutation run recorded in the PR.
No device: ``urllib.request.urlopen`` is mocked.
"""
from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends import ultimate64_probe as probe_mod
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    _wire_hex16,
)
from c64_test_harness.backends.ultimate64_probe import ProbeResult, liveness_probe

_SRC = Path(__file__).resolve().parent.parent / "src" / "c64_test_harness"
_STRICT = re.compile(r"\A[0-9A-F]{4}\Z")


class _Resp:
    status = 200

    def __init__(self, body: bytes = b"") -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def read(self) -> bytes:
        return self._body


def _wire(body: bytes = b""):
    seen: list[object] = []

    def _fake(req, timeout=None):
        seen.append(req)
        return _Resp(body)

    return MagicMock(side_effect=_fake), seen


def _address_args(req) -> list[str]:
    query = urllib.parse.urlsplit(req.get_full_url()).query
    return urllib.parse.parse_qs(query, keep_blank_values=True).get("address", [])


@pytest.fixture
def client() -> Ultimate64Client:
    # Explicit threshold: construction issues no HTTP.
    return Ultimate64Client("h", write_mem_query_threshold=48, warn_unlocked=False,
                            temp_hygiene=False)


_MALFORMED = [
    None, "0x0400", "0400", "$0400", "", 1024.0, b"\x04\x00", -1, 0x10000, 0xFFFFFFFF,
]


# --------------------------------------------------------------------------- #
# Local refusal                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("bad", _MALFORMED, ids=repr)
@pytest.mark.parametrize("size", [1, 48, 49, 200], ids=lambda n: f"{n}B")
def test_write_mem_refuses_a_malformed_address_before_any_request(client, bad, size):
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock), pytest.raises((ValueError, TypeError)):
        client.write_mem(bad, bytes(size))  # type: ignore[arg-type]
    assert seen == []


@pytest.mark.parametrize("bad", _MALFORMED, ids=repr)
def test_read_mem_refuses_a_malformed_address_before_any_request(client, bad):
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock), pytest.raises((ValueError, TypeError)):
        client.read_mem(bad, 1)  # type: ignore[arg-type]
    assert seen == []


# --------------------------------------------------------------------------- #
# The formatter                                                               #
# --------------------------------------------------------------------------- #

def test_wire_hex16_is_strict_and_round_trips_for_every_address():
    bad = [a for a in range(0x10000)
           if not _STRICT.match(_wire_hex16(a)) or int(_wire_hex16(a), 16) != a]
    assert bad == []


@pytest.mark.parametrize("bad", _MALFORMED, ids=repr)
def test_wire_hex16_refuses_what_it_cannot_format_strictly(bad):
    with pytest.raises((ValueError, TypeError)):
        _wire_hex16(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize("text, ok", [
    ("0400", True), ("FFFF", True), ("0000", True),
    ("0x0400", False), ("400", False), ("ZZZZ", False), ("04000", False),
    ("ffff", False), ("", False), ("0400\n", False),
])
def test_the_strict_pattern_itself(text, ok):
    """Vacuity guard for every check below: the pattern rejects the forms
    the firmware would silently turn into $0000."""
    assert bool(_STRICT.match(text)) is ok


# --------------------------------------------------------------------------- #
# What reaches the wire                                                       #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("address", [0x0000, 0x0001, 0x00FF, 0x0334, 0x0400, 0xC000, 0xFFFF],
                         ids=lambda a: f"${a:04X}")
@pytest.mark.parametrize("size", [1, 48, 49], ids=lambda n: f"{n}B")
def test_every_writemem_request_carries_a_strict_equal_address(client, address, size):
    size = min(size, 0x10000 - address)
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock):
        client.write_mem(address, bytes(size))
    assert len(seen) == 1
    req = seen[0]
    assert req.get_method() == ("PUT" if size <= 48 else "POST")
    args = _address_args(req)
    assert len(args) == 1, req.get_full_url()
    assert _STRICT.match(args[0]), req.get_full_url()
    assert int(args[0], 16) == address


def test_zero_page_itself_is_still_writable(client):
    """Positive control: strictness must not block a deliberate $0000 write."""
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock):
        client.write_mem(0x0000, b"\x2f")
    assert _address_args(seen[0]) == ["0000"]


def test_liveness_probe_and_its_restore_send_strict_addresses():
    pattern = bytes((i ^ 0x5A) & 0xFF for i in range(128))
    bodies = iter([b'{"firmware_version": "1.1.0"}', bytes(128), b"", pattern, b""])
    seen: list[object] = []

    def _fake(req, timeout=None):
        seen.append(req)
        return _Resp(next(bodies))

    reachable = ProbeResult(host="h", port=80, reachable=True, ping_ok=None,
                            port_ok=True, api_ok=True, latency_ms=1.0, error=None)
    with patch.object(probe_mod, "probe_u64", return_value=reachable), \
            patch("urllib.request.urlopen", MagicMock(side_effect=_fake)):
        r = liveness_probe("h")
    assert r.healthy is True
    memory_requests = [q for q in seen if "/v1/machine:" in q.get_full_url()]
    # readmem, writemem, readback, restore writemem
    assert len(memory_requests) == 4
    for req in memory_requests:
        assert _address_args(req) == ["0334"], req.get_full_url()


# --------------------------------------------------------------------------- #
# No hand-built address query anywhere in src                                  #
# --------------------------------------------------------------------------- #

#: A line that builds an ``address`` query argument in code: a dict key
#: ``"address":`` / ``'address':``, or an f-string / literal starting
#: ``address=``.  Prose in docstrings uses ``address=`` inside double
#: backticks and is excluded below.
_BUILDS_ADDRESS = re.compile(r"""["']address["']\s*:|["']address=|[?&]address=\{""")


def _unguarded_address_builders(text: str) -> list[str]:
    hits = []
    for line in text.splitlines():
        if "``" in line or line.lstrip().startswith("#"):
            continue
        if _BUILDS_ADDRESS.search(line) and "_wire_hex16(" not in line:
            hits.append(line.strip())
    return hits


def _all_address_builders(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines()
            if "``" not in line and not line.lstrip().startswith("#")
            and _BUILDS_ADDRESS.search(line)]


def test_no_src_module_builds_an_address_query_without_the_formatter():
    offenders = {}
    for path in sorted(_SRC.rglob("*.py")):
        hits = _unguarded_address_builders(path.read_text())
        if hits:
            offenders[str(path.relative_to(_SRC))] = hits
    assert offenders == {}


def test_the_scan_sees_the_real_builders():
    """Vacuity guard: the scan finds the sites that exist today (read_mem,
    write_mem's two forms, the probe's three), so a scan that matches
    nothing cannot pass the test above."""
    found = sum(len(_all_address_builders(p.read_text())) for p in _SRC.rglob("*.py"))
    assert found >= 6, found


@pytest.mark.parametrize("line", [
    'query = {"address": "%04X" % address, "data": payload.hex()}',
    "query={'address': hex(addr)}",
    'post_query = f"address=0x{probe_addr:04X}"',
    'url = f"/v1/machine:writemem?address={addr:x}&data={hexdata}"',
])
def test_the_scan_fires_on_hand_built_queries(line):
    assert _unguarded_address_builders(line) == [line]


@pytest.mark.parametrize("line", [
    'query = {"address": _wire_hex16(address), "length": "%d" % length}',
    'addr_query = f"address={_wire_hex16(probe_addr)}&length={probe_len}"',
    "    sends ``address=0xC000`` and gets a 400",
])
def test_the_scan_passes_guarded_builders_and_prose(line):
    assert _unguarded_address_builders(line) == []
