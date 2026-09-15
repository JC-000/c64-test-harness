"""``Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD`` does what its docstring says (#249).

Before #249 the class attribute was never read: ``__init__`` always set the
per-instance ``write_mem_query_threshold`` from the kwarg or the capability
probe, so a caller "poking the class" -- the path the attribute's own comment
offered -- changed nothing and got no error.  A consumer lane carried six such
lines as its safety control.

The attribute is now honoured, with a stated precedence:

1. the ``write_mem_query_threshold=`` kwarg (the supported path);
2. a poke of ``WRITE_MEM_QUERY_THRESHOLD`` -- on the class, on a subclass, or
   on an instance after construction;
3. the capability grade (128 leak-prone / unknown, 48 post-safe).

Every expected threshold below is chosen to differ from *both* grade values
(48 and 128), so a test cannot pass because the probe happened to land on the
number it asserts.  No device: ``urllib.request.urlopen`` is mocked.
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.u64_capabilities import (
    THRESHOLD_POST_RISKY,
    THRESHOLD_POST_SAFE,
)
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

_LOGGER = "c64_test_harness.backends.ultimate64_client"


class _Resp:
    def __init__(self, body: bytes = b"") -> None:
        self._body = body
        self.status = 200

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _device(firmware: str | None):
    """urlopen double: ``/v1/info`` answers *firmware*; everything else 200 empty.

    ``firmware=None`` makes the info GET fail, which grades conservatively.
    """
    wire: list[object] = []

    def _fake(req, timeout=None):
        wire.append(req)
        url = req.get_full_url()
        if url.endswith("/v1/info"):
            if firmware is None:
                raise OSError("no device")
            product = "C64 Ultimate" if firmware.startswith("1.") else "Ultimate 64"
            return _Resp(json.dumps(
                {"firmware_version": firmware, "product": product}
            ).encode())
        return _Resp()

    return MagicMock(side_effect=_fake), wire


def _writemem_methods(wire: list[object]) -> list[str]:
    return [
        r.get_method() for r in wire
        if "/v1/machine:writemem" in r.get_full_url()
    ]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("U64_AUTO_TEMP_GC", "U64_TEMP_GC_REQUIRED", "U64_TEMP_GC_BUDGET"):
        monkeypatch.delenv(var, raising=False)


def _boundary(client: Ultimate64Client, n: int) -> list[str]:
    """Wire methods for an n-byte and an (n+1)-byte write."""
    mock, wire = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        client.write_mem(0x0400, bytes(n))
        client.write_mem(0x0400, bytes(n + 1))
    return _writemem_methods(wire)


# --------------------------------------------------------------------------- #
# The name stays importable and keeps its value                               #
# --------------------------------------------------------------------------- #

def test_constant_is_still_the_post_safe_int():
    value = Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD
    assert isinstance(value, int)
    assert value == THRESHOLD_POST_SAFE == 48
    # Downstream code uses it as a length.
    assert len(bytes(range(value))) == 48


# --------------------------------------------------------------------------- #
# Positive control: unpoked, the grade decides                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("firmware, expected", [
    ("3.15", THRESHOLD_POST_SAFE),
    ("1.1.0", THRESHOLD_POST_RISKY),
    (None, THRESHOLD_POST_RISKY),
])
def test_unpoked_client_takes_the_grade(firmware, expected):
    """The shipped value must not be mistaken for a poke: on a leak-prone
    grade the client stays at 128, not the constant's 48."""
    mock, _ = _device(firmware)
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == expected


# --------------------------------------------------------------------------- #
# The pokes are honoured                                                      #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("firmware", ["3.15", "1.1.0", None])
@pytest.mark.parametrize("poked", [100, 32])
def test_class_poke_is_honoured(monkeypatch, firmware, poked):
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", poked)
    mock, _ = _device(firmware)
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == poked
    assert _boundary(c, poked) == ["PUT", "POST"]


def test_class_poke_to_the_shipped_value_is_honoured(monkeypatch):
    """Re-assigning 48 is a poke too: on a leak-prone grade it must lower the
    threshold to 48, not be mistaken for the untouched default."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 48)
    mock, _ = _device("1.1.0")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 48


def test_subclass_override_is_honoured():
    class Conservative(Ultimate64Client):
        WRITE_MEM_QUERY_THRESHOLD = 100

    mock, _ = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        c = Conservative("h", warn_unlocked=False)
        base = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 100
    # The subclass does not leak into the base class.
    assert base.write_mem_query_threshold == THRESHOLD_POST_SAFE


def test_instance_poke_is_honoured():
    mock, _ = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == THRESHOLD_POST_SAFE
    c.WRITE_MEM_QUERY_THRESHOLD = 100
    assert c.write_mem_query_threshold == 100
    assert _boundary(c, 100) == ["PUT", "POST"]
    # The poke is per instance: the class keeps the shipped value.
    assert Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD == 48


def test_kwarg_beats_a_class_poke(monkeypatch, caplog):
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 100)
    mock, wire = _device("3.15")
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", write_mem_query_threshold=64, warn_unlocked=False)
    assert c.write_mem_query_threshold == 64
    # The kwarg's no-HTTP-at-construction contract still holds.
    assert wire == []
    assert any("WRITE_MEM_QUERY_THRESHOLD" in r.getMessage() and "ignored" in r.getMessage()
               for r in caplog.records)


def test_class_poke_still_probes_so_hygiene_still_arms(monkeypatch):
    """Honouring a poke must not silently disarm /Temp hygiene the way an
    explicit kwarg does: a lane that poked 128 against a C64U was armed
    before #249 (the probe ran) and must stay armed."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 100)
    mock, _ = _device("1.1.0")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c._capabilities is not None
    assert c._capabilities.firmware_version == "1.1.0"
    assert c.temp_hygiene_armed is True


@pytest.mark.parametrize("bad, exc", [
    (True, TypeError), ("128", TypeError), (128.0, TypeError), (-1, ValueError),
])
def test_a_nonsense_poke_fails_loudly(monkeypatch, bad, exc):
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", bad)
    mock, _ = _device("3.15")
    with patch("urllib.request.urlopen", mock), pytest.raises(exc):
        Ultimate64Client("h", warn_unlocked=False)


def test_poke_is_announced(monkeypatch, caplog):
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 100)
    mock, _ = _device("3.15")
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        Ultimate64Client("h", warn_unlocked=False)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("WRITE_MEM_QUERY_THRESHOLD" in m and "write_mem_query_threshold=" in m
               for m in msgs), msgs


def test_docstring_no_longer_offers_a_path_that_does_nothing():
    import inspect

    from c64_test_harness.backends import ultimate64_client as mod

    import re

    # Comment prose wraps across ``#:`` lines; fold it before matching.
    flat = re.sub(r"\s*#:\s*|\s+", " ", inspect.getsource(mod.Ultimate64Client))
    stale = "retained for backwards compatibility with callers that poke the class"
    assert stale not in flat
    # Vacuity guard: the fold really does join a wrapped comment.
    wrapped = "    #: attribute is retained for backwards compatibility with callers that\n    #: poke the class."
    assert stale in re.sub(r"\s*#:\s*|\s+", " ", wrapped)
    # And the supported path is what the attribute's comment now names.
    assert "Precedence" in flat and "write_mem_query_threshold=" in flat
