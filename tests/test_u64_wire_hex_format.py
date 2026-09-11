"""Wire-format tests for the ``machine:readmem`` / ``machine:writemem`` query strings.

Issue #272 / upstream GideonZ/1541ultimate#884: the firmware's hex parser
no longer accepts a ``0x`` prefix on ``address`` (``:readmem``,
``:writemem``) — a prefixed value is an HTTP 400.

These tests assert the URL **as constructed**, captured at
``urllib.request.urlopen``, with no device and no round trip.  That is
deliberate: every pre-existing test for these methods round-trips through a
fake that accepts either form, which is exactly why the prefix went
unnoticed.  A test that only checks the returned bytes cannot fail on this.
"""
from __future__ import annotations

import urllib.parse
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64Error,
    Ultimate64WireFormatError,
    _wire_hex16,
)


class _FakeResponse:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _capture(response_body: bytes = b"", status: int = 200):
    captured: list[object] = []

    def _fake(req, timeout=None):
        captured.append(req)
        return _FakeResponse(response_body, status=status)

    return MagicMock(side_effect=_fake), captured


def _query_of(req) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlsplit(req.full_url).query)


def _client() -> Ultimate64Client:
    c = Ultimate64Client("dev.invalid")
    # Pin the PUT/POST switch so each path is exercised deliberately rather
    # than depending on a probed capability grade.
    c.write_mem_query_threshold = 4
    return c


# ------------------------------------------------------------- the formatter
def test_wire_hex16_is_bare_uppercase_four_digits():
    assert _wire_hex16(0x0400) == "0400"
    assert _wire_hex16(0) == "0000"
    assert _wire_hex16(0xFFFF) == "FFFF"
    assert _wire_hex16(0xC000) == "C000"


@pytest.mark.parametrize("value", [0, 0x0400, 0xDE00, 0xFFFF])
def test_wire_hex16_never_emits_a_prefix(value):
    assert not _wire_hex16(value).lower().startswith("0x")


# ------------------------------------------------------------ the call sites
def test_read_mem_sends_bare_hex_address():
    c = _client()
    mock, captured = _capture(b"\x00\x00")
    with patch("urllib.request.urlopen", mock):
        c.read_mem(0xC000, 2)
    url = captured[0].full_url
    assert "0x" not in url, url
    assert _query_of(captured[0])["address"] == ["C000"]


def test_write_mem_put_path_sends_bare_hex_address():
    c = _client()
    mock, captured = _capture()
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0x0400, b"\xAA\xBB")
    req = captured[0]
    assert req.get_method() == "PUT"
    assert "0x" not in req.full_url, req.full_url
    q = _query_of(req)
    assert q["address"] == ["0400"]
    assert q["data"] == ["AABB"]


def test_write_mem_post_path_sends_bare_hex_address():
    c = _client()
    mock, captured = _capture()
    with patch("urllib.request.urlopen", mock):
        c.write_mem(0xDE00, b"\x01\x02\x03\x04\x05\x06")
    req = captured[0]
    assert req.get_method() == "POST"
    assert "0x" not in req.full_url, req.full_url
    assert _query_of(req)["address"] == ["DE00"]


# -------------------------------------------------------------- the diagnostic
def _raises_wire_format(fn) -> Ultimate64WireFormatError:
    mock, _ = _capture(b"Invalid address", status=400)
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64WireFormatError) as exc:
            fn()
    return exc.value


def test_read_mem_400_names_the_contract_change():
    err = _raises_wire_format(lambda: _client().read_mem(0xC000, 2))
    assert err.status == 400
    text = str(err)
    assert "bare hex" in text.lower()
    assert "#272" in text
    assert "884" in text


def test_write_mem_put_400_names_the_contract_change():
    err = _raises_wire_format(lambda: _client().write_mem(0x0400, b"\xAA"))
    assert "#272" in str(err)


def test_write_mem_post_400_names_the_contract_change():
    err = _raises_wire_format(lambda: _client().write_mem(0x0400, b"\x01" * 8))
    assert "#272" in str(err)


def test_wire_format_error_is_an_ultimate64_error():
    """Callers catching the base class keep working."""
    assert issubclass(Ultimate64WireFormatError, Ultimate64Error)


def test_device_body_is_preserved_on_the_mapped_error():
    err = _raises_wire_format(lambda: _client().read_mem(0xC000, 2))
    assert err.body == "Invalid address"
    assert "Invalid address" in str(err)


def test_non_400_from_writemem_is_not_mapped():
    """A 404 on writemem is the fw-3.14d degraded path, not a wire-format fault."""
    c = _client()
    mock, _ = _capture(b"nope", status=404)
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64Error) as exc:
            c.write_mem(0x0400, b"\xAA")
    assert not isinstance(exc.value, Ultimate64WireFormatError)
    assert exc.value.status == 404


def test_400_from_an_unrelated_endpoint_is_not_mapped():
    """Only the hex-parsing routes carry the #884 contract."""
    c = _client()
    mock, _ = _capture(b"bad", status=400)
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64Error) as exc:
            c.reset()
    assert not isinstance(exc.value, Ultimate64WireFormatError)


def test_debugreg_400_is_mapped_too():
    """``:debugreg`` is the third route #884 tightened.

    But it takes no address and no length — ``get_debug_register`` sends
    no arguments at all — so the message must not blame the 0x prefix or
    an out-of-range address.  Asserting a cause that cannot apply is the
    same misdiagnosis this whole change exists to prevent, pointed the
    other way.
    """
    c = _client()
    mock, _ = _capture(b"bad value", status=400)
    with patch("urllib.request.urlopen", mock):
        with pytest.raises(Ultimate64WireFormatError) as exc:
            c.get_debug_register()
    text = str(exc.value)
    assert "#272" in text
    assert "884" in text
    assert "this route needs bare hexadecimal" not in text
    assert "out-of-range address/length" not in text
    assert "not the cause here" in text
    # The advice that is true of any 400 from these routes survives.
    assert "do not reboot()" in text


def test_readmem_400_still_names_the_prefix_as_a_candidate():
    """The route that *does* carry a harness-formatted address keeps the hint."""
    err = _raises_wire_format(lambda: _client().read_mem(0xC000, 2))
    text = str(err)
    assert "this route needs bare hexadecimal" in text
    assert "out-of-range address/length" in text


# ------------------------------------------------- the shape urllib really takes
def test_mapping_fires_on_a_raised_http_error_not_just_a_returned_400():
    """Production never sees a *returned* 400 — ``urllib`` raises.

    Every other test here fakes the status as a return value, which is
    the entrance ``urlopen`` does not use.  A suite that only exercises
    a shape production never takes is #272's own failure in different
    clothes, so this drives the real one.
    """
    import urllib.error
    from io import BytesIO

    def _raising(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {}, BytesIO(b"Invalid address")
        )

    c = _client()
    with patch("urllib.request.urlopen", MagicMock(side_effect=_raising)):
        with pytest.raises(Ultimate64WireFormatError) as exc:
            c.read_mem(0xC000, 2)
    assert exc.value.status == 400
    assert "#272" in str(exc.value)
    assert exc.value.body == "Invalid address"


def test_raised_http_error_on_an_unrelated_route_is_still_not_mapped():
    """The raised entrance must not over-map either."""
    import urllib.error
    from io import BytesIO

    def _raising(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad", {}, BytesIO(b"no"))

    c = _client()
    with patch("urllib.request.urlopen", MagicMock(side_effect=_raising)):
        with pytest.raises(Ultimate64Error) as exc:
            c.reset()
    assert not isinstance(exc.value, Ultimate64WireFormatError)


# ------------------------------------------------------- the choke point guards
@pytest.mark.parametrize("bad", [0x10000, -1, 0x1FFFF])
def test_wire_hex16_rejects_values_that_do_not_fit_16_bits(bad):
    """A five-digit address must not leave the single formatting choke point."""
    with pytest.raises(ValueError):
        _wire_hex16(bad)


# ===================================================================== #
# liveness_probe builds its own query strings and never goes through    #
# Ultimate64Client, so it carries the same #884 exposure independently  #
# — and it is the instrument a lane reaches for when it already         #
# suspects a wedge, so a wire-format 400 must not read as "degraded".   #
# ===================================================================== #
from c64_test_harness.backends.ultimate64_probe import (  # noqa: E402
    LivenessResult,
    ProbeResult,
    _restore_quiet,
    liveness_probe,
)

_PROBE_LEN = 128
_PROBE_ADDR = 0x0334
_PROBE_PATTERN = bytes((i ^ 0x5A) & 0xFF for i in range(_PROBE_LEN))

_REACHABLE = ProbeResult(
    host="10.0.0.1", port=80, reachable=True,
    ping_ok=None, port_ok=True, api_ok=True,
    latency_ms=1.0, error=None,
)


def _probe_response(status: int, body: bytes) -> MagicMock:
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = body
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _healthy_sequence() -> list[MagicMock]:
    """info, readmem, POST writemem, readback, restore POST — all 200."""
    import json as _json

    return [
        _probe_response(200, _json.dumps({"firmware_version": "V3.15"}).encode()),
        _probe_response(200, b"\x00" * _PROBE_LEN),
        _probe_response(200, b""),
        _probe_response(200, _PROBE_PATTERN),
        _probe_response(200, b""),
    ]


def _urls(mock_urlopen) -> list[str]:
    return [call.args[0].full_url for call in mock_urlopen.call_args_list]


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_liveness_probe_sends_bare_hex_on_every_request(mock_urlopen, mock_probe):
    """All three of the probe's own query builders, in one pass."""
    mock_probe.return_value = _REACHABLE
    mock_urlopen.side_effect = _healthy_sequence()

    r = liveness_probe("10.0.0.1")
    assert r.healthy is True

    urls = _urls(mock_urlopen)
    assert len(urls) == 5
    mem_urls = [u for u in urls if "machine:" in u]
    assert len(mem_urls) == 4, urls
    for url in mem_urls:
        assert "0x" not in url, url
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        assert q["address"] == ["0334"], url


@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_restore_quiet_sends_bare_hex(mock_urlopen):
    """The restore POST is the third site and the easiest one to miss."""
    mock_urlopen.side_effect = [_probe_response(200, b"")]
    _restore_quiet("10.0.0.1", 80, None, 1.0, _PROBE_ADDR, b"\x00" * 4)
    url = _urls(mock_urlopen)[0]
    assert "0x" not in url, url
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["address"] == ["0334"]


# ------------------------------- the probe's own wire-format diagnostic
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_readmem_400_is_tagged_wire_format_not_unknown(mock_urlopen, mock_probe):
    """A 400 on the probe's readmem is the #884 signature, not a sick device."""
    import json as _json

    mock_probe.return_value = _REACHABLE
    mock_urlopen.side_effect = [
        _probe_response(200, _json.dumps({"firmware_version": "V3.16"}).encode()),
        _probe_response(400, b"Invalid address"),
    ]
    r = liveness_probe("10.0.0.1")
    assert isinstance(r, LivenessResult)
    assert r.healthy is False
    assert r.failure == "wire_format"
    assert "#272" in r.recommendation
    assert "884" in r.recommendation
    # The whole point: it must not send anyone to the power switch, and
    # must say so positively — the absence of the word "power-cycle" is
    # not the same as telling the reader to keep their hands off it.
    assert "power-cycle" not in r.recommendation
    assert "do not reboot it" in r.recommendation
    assert "do not send anyone to the power switch" in r.recommendation


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_writemem_400_is_tagged_wire_format_not_degraded(mock_urlopen, mock_probe):
    """A 400 on the POST must not reach the writemem-degraded recommendation."""
    import json as _json

    mock_probe.return_value = _REACHABLE
    mock_urlopen.side_effect = [
        _probe_response(200, _json.dumps({"firmware_version": "V3.16"}).encode()),
        _probe_response(200, b"\x00" * _PROBE_LEN),
        _probe_response(400, b"Invalid address"),
    ]
    r = liveness_probe("10.0.0.1")
    assert r.healthy is False
    assert r.writemem_ok is False
    assert r.failure == "wire_format"
    assert "#272" in r.recommendation
    assert "power-cycle" not in r.recommendation
    assert "do not reboot it" in r.recommendation
    assert "do not send anyone to the power switch" in r.recommendation
    # It must not reproduce the fw-3.14d degraded diagnosis...
    assert "writemem-degraded" not in r.recommendation
    # ...and must say positively that the device is not the fault.
    assert "NOT degraded" in r.recommendation


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_writemem_404_still_reports_degraded(mock_urlopen, mock_probe):
    """The 3.14d degraded diagnosis survives — 404 is a different fault."""
    import json as _json

    mock_probe.return_value = _REACHABLE
    mock_urlopen.side_effect = [
        _probe_response(200, _json.dumps({"firmware_version": "V3.14d"}).encode()),
        _probe_response(200, b"\x00" * _PROBE_LEN),
        _probe_response(404, b""),
    ]
    r = liveness_probe("10.0.0.1")
    assert r.failure == "writemem_404"
    assert "power-cycle" in r.recommendation


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_non_400_readmem_status_still_reports_unknown(mock_urlopen, mock_probe):
    """Only 400 carries the #884 signature; 500 keeps its old tag."""
    import json as _json

    mock_probe.return_value = _REACHABLE
    mock_urlopen.side_effect = [
        _probe_response(200, _json.dumps({"firmware_version": "V3.14d"}).encode()),
        _probe_response(500, b""),
    ]
    r = liveness_probe("10.0.0.1")
    assert r.failure == "unknown"


# --------------------------------------------- assert_healthy must agree
@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_assert_healthy_raises_wire_format_not_degraded(mock_urlopen, mock_probe):
    """``assert_healthy`` names the fault by its exception type too.

    Raising ``U64WritememDegradedError`` for a wire-format 400 would put
    the wrong word in front of the reader at the one moment they are
    deciding whether the device is broken.
    """
    import json as _json

    from c64_test_harness.backends.ultimate64_client import U64WritememDegradedError

    mock_probe.return_value = _REACHABLE

    def _dispatch(req, timeout=None):
        url = req.full_url
        if "machine:readmem" in url:
            return _probe_response(400, b"Invalid address")
        return _probe_response(
            200, _json.dumps({"firmware_version": "V3.16"}).encode()
        )

    mock_urlopen.side_effect = _dispatch
    c = Ultimate64Client("10.0.0.1")
    with pytest.raises(Ultimate64WireFormatError) as exc:
        c.assert_healthy()
    assert not isinstance(exc.value, U64WritememDegradedError)
    assert "#272" in str(exc.value)
    # The LivenessResult is still reachable for callers that inspect it.
    assert exc.value.result is not None
    assert exc.value.result.failure == "wire_format"


# ------------------------------------------------------- one formatter only
def test_probe_uses_the_client_formatter_object():
    """Not 'formats the same' — literally the same function.

    Three call sites each formatting their own hex is how this bug
    survived; a fourth copy in the probe is how it would come back.
    """
    from c64_test_harness.backends import ultimate64_probe

    assert ultimate64_probe._wire_hex16 is _wire_hex16


def test_wire_format_error_is_importable_from_the_package_root():
    """The diagnostic is only useful if a caller can catch it easily."""
    import c64_test_harness

    assert c64_test_harness.Ultimate64WireFormatError is Ultimate64WireFormatError
    assert "Ultimate64WireFormatError" in c64_test_harness.__all__
    assert issubclass(c64_test_harness.Ultimate64WireFormatError, Ultimate64Error)


@patch("c64_test_harness.backends.ultimate64_probe.probe_u64")
@patch("c64_test_harness.backends.ultimate64_probe.urllib.request.urlopen")
def test_readback_400_is_unknown_and_still_restores(mock_urlopen, mock_probe):
    """The readback checkpoint has no wire-format mapping, on purpose.

    It reuses the same query the readmem already got a 200 for, so a 400
    there cannot be the #272 prefix.  This pins what the generic branch
    does instead — tag ``unknown`` and put the original bytes back —
    because that is the behaviour the deleted mapping was relying on.
    """
    import json as _json

    mock_probe.return_value = _REACHABLE
    original = b"\x11" * _PROBE_LEN
    mock_urlopen.side_effect = [
        _probe_response(200, _json.dumps({"firmware_version": "V3.16"}).encode()),
        _probe_response(200, original),      # readmem
        _probe_response(200, b""),           # POST writemem
        _probe_response(400, b"nope"),       # readback
        _probe_response(200, b""),           # restore POST
    ]
    r = liveness_probe("10.0.0.1")
    assert r.failure == "unknown"
    assert "#272" not in (r.recommendation or "")

    # The restore actually went out, with the original bytes.
    calls = mock_urlopen.call_args_list
    assert len(calls) == 5, [c.args[0].full_url for c in calls]
    restore_req = calls[4].args[0]
    assert restore_req.get_method() == "POST"
    assert "machine:writemem" in restore_req.full_url
    assert restore_req.data == original
