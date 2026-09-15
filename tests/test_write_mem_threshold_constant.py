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

@pytest.mark.parametrize("poked", [1, 32, 100, 128])
def test_class_poke_is_honoured_on_a_post_safe_grade(monkeypatch, poked):
    """Review round 1: a post-safe grade honours any poke in 1..128."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", poked)
    mock, _ = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == poked
    assert _boundary(c, poked) == ["PUT", "POST"]


def _refused(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.levelno == logging.WARNING and "refused" in r.getMessage()
            and "WRITE_MEM_QUERY_THRESHOLD" in r.getMessage()]


@pytest.mark.parametrize("firmware", ["1.1.0", None])
@pytest.mark.parametrize("poked", [0, 1, 16, 48, 100, 127])
def test_downward_class_poke_is_refused_when_not_post_safe(monkeypatch, caplog, firmware, poked):
    """Review round 1 finding 1 (hardware rule 8): below 128 on a grade that
    is not post-safe a poke only moves writes onto the leaking POST path, so
    it is refused -- the threshold stays 128 and a WARNING says why."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", poked)
    mock, _ = _device(firmware)
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == THRESHOLD_POST_RISKY
    assert _boundary(c, THRESHOLD_POST_RISKY) == ["PUT", "POST"]
    msgs = _refused(caplog)
    assert msgs and all("/Temp" in m for m in msgs), msgs


def test_upward_poke_to_the_cap_is_honoured_when_not_post_safe(monkeypatch, caplog):
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 128)
    mock, _ = _device("1.1.0")
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 128
    assert _refused(caplog) == []


@pytest.mark.parametrize("firmware", ["3.15", "1.1.0", None])
@pytest.mark.parametrize("poked", [129, 200, 1000])
def test_poke_above_the_put_cap_is_clamped(monkeypatch, caplog, firmware, poked):
    """Review round 1 finding 2: the firmware refuses a ``data=`` PUT over
    128 bytes on every grade, so a poke above 128 is clamped to 128."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", poked)
    mock, _ = _device(firmware)
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 128
    assert _boundary(c, 128) == ["PUT", "POST"]
    assert any("clamped" in m and "128" in m for m in
               (r.getMessage() for r in caplog.records if r.levelno == logging.WARNING))


def test_reviewer_wire_experiment_downward_poke_no_longer_posts(monkeypatch):
    """The review's measurement: leak-prone grade, class poke 16, a direct
    100-byte write_mem went POST (one attachment).  It must stay PUT."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 16)
    mock, wire = _device("1.1.0")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False, temp_hygiene=False)
        wire.clear()
        c.write_mem(0x4000, bytes(100))
    assert _writemem_methods(wire) == ["PUT"]


def test_reviewer_wire_experiment_huge_poke_no_longer_sends_an_oversized_put(monkeypatch):
    """The review's measurement: poke 1000 sent a 200-byte ``data=`` PUT the
    firmware refuses.  Clamped, a 200-byte write is a POST on a post-safe grade."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 1000)
    mock, wire = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
        wire.clear()
        c.write_mem(0x4000, bytes(200))
    assert _writemem_methods(wire) == ["POST"]


def test_class_poke_to_the_shipped_value_counts_as_a_poke(monkeypatch, caplog):
    """Re-assigning a plain 48 is a poke, not the untouched default: on a
    leak-prone grade it is seen (and refused), not silently ignored."""
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 48)
    mock, _ = _device("1.1.0")
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 128
    assert _refused(caplog), "a plain 48 was mistaken for the shipped default"


def test_restoring_the_saved_original_undoes_a_class_poke(caplog):
    """Review round 1 nit 3: the documented undo.  Only the saved original
    object clears a poke; monkeypatch does this automatically."""
    original = Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD
    try:
        Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD = 100
        Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD = 48  # still a poke
        mock, _ = _device("1.1.0")
        with caplog.at_level(logging.WARNING, logger=_LOGGER), \
                patch("urllib.request.urlopen", mock):
            Ultimate64Client("h", warn_unlocked=False)
        assert _refused(caplog)
        caplog.clear()
    finally:
        Ultimate64Client.WRITE_MEM_QUERY_THRESHOLD = original
    mock, _ = _device("1.1.0")
    with caplog.at_level(logging.WARNING, logger=_LOGGER), \
            patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    assert c.write_mem_query_threshold == 128
    assert not any("WRITE_MEM_QUERY_THRESHOLD" in r.getMessage() for r in caplog.records)


def test_constant_docstring_documents_the_undo():
    import inspect
    import re

    from c64_test_harness.backends import ultimate64_client as mod

    flat = re.sub(r"\s*#:\s*|\s+", " ", inspect.getsource(mod.Ultimate64Client))
    assert "monkeypatch" in flat and "saved original" in flat


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


@pytest.mark.parametrize("firmware", ["1.1.0", "unprobed"])
def test_downward_instance_poke_is_refused_when_not_post_safe(caplog, firmware):
    """Same rule for an instance poke; an unprobed (pinned) client has no
    grade at all and is treated as not post-safe."""
    if firmware == "unprobed":
        c = Ultimate64Client("h", write_mem_query_threshold=128, warn_unlocked=False)
    else:
        mock, _ = _device(firmware)
        with patch("urllib.request.urlopen", mock):
            c = Ultimate64Client("h", warn_unlocked=False)
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        c.WRITE_MEM_QUERY_THRESHOLD = 16
    assert c.write_mem_query_threshold == 128
    assert _refused(caplog)


def test_instance_poke_above_the_cap_is_clamped():
    mock, _ = _device("3.15")
    with patch("urllib.request.urlopen", mock):
        c = Ultimate64Client("h", warn_unlocked=False)
    c.WRITE_MEM_QUERY_THRESHOLD = 500
    assert c.write_mem_query_threshold == 128


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
    monkeypatch.setattr(Ultimate64Client, "WRITE_MEM_QUERY_THRESHOLD", 128)
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
