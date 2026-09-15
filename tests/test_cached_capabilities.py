"""``Ultimate64Client.cached_capabilities`` -- the non-probing grade (#291, #265).

``capabilities`` probes (``GET /v1/info``) on a cold cache.  Several readers
must never issue HTTP of their own -- hygiene arming, the grading log line,
the transport's chunking decision, and the refusal message -- and before
#291 each reached for the private ``_capabilities`` to get that.  #265 was
the one that did not: ``_refuse_or_warn`` interpolated the probing property,
so building an exception message on a client that had never probed issued a
``GET /v1/info`` and mutated the cache.

No device: ``urllib.request.urlopen`` is mocked and every test asserts on the
wire it saw.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import (
    Ultimate64Client,
    Ultimate64TempHygieneError,
)

LEAKY = DeviceCapabilities.from_info({"firmware_version": "1.1.0", "product": "C64 Ultimate"})
FIXED = DeviceCapabilities.from_info({"firmware_version": "3.15", "product": "Ultimate 64"})

_SRC = Path(__file__).resolve().parent.parent / "src" / "c64_test_harness"


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


def _wire():
    seen: list[object] = []

    def _fake(req, timeout=None):
        seen.append(req)
        return _Resp(b'{"firmware_version": "1.1.0", "product": "C64 Ultimate"}')

    return MagicMock(side_effect=_fake), seen


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch):
    for var in ("U64_AUTO_TEMP_GC", "U64_TEMP_GC_REQUIRED", "U64_TEMP_GC_BUDGET"):
        monkeypatch.delenv(var, raising=False)


def _pinned(**kw) -> Ultimate64Client:
    """A client whose construction issues no HTTP (threshold pinned)."""
    kw.setdefault("write_mem_query_threshold", 128)
    return Ultimate64Client("fake-host", warn_unlocked=False, **kw)


# --------------------------------------------------------------------------- #
# The accessor                                                                #
# --------------------------------------------------------------------------- #

def test_cached_capabilities_is_none_on_a_cold_client_and_issues_no_http():
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock):
        c = _pinned()
        assert c.cached_capabilities is None
        assert c.cached_capabilities is None
    assert seen == []
    # And reading it did not fill the cache behind the caller's back.
    assert c._capabilities is None


def test_cached_capabilities_returns_the_cached_grade():
    c = _pinned()
    c._capabilities = LEAKY
    assert c.cached_capabilities is LEAKY


def test_cached_capabilities_follows_a_real_probe():
    """Positive control for the no-HTTP test: the *probing* property does
    issue the GET on the same mock, and the accessor then sees its result."""
    mock, seen = _wire()
    with patch("urllib.request.urlopen", mock):
        c = _pinned()
        caps = c.capabilities
    assert len(seen) == 1 and seen[0].get_full_url().endswith("/v1/info")
    assert c.cached_capabilities is caps
    assert caps.firmware_version == "1.1.0"


def test_cached_capabilities_is_read_only():
    c = _pinned()
    with pytest.raises(AttributeError):
        c.cached_capabilities = FIXED  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# #265: the refusal message never probes                                      #
# --------------------------------------------------------------------------- #

def _blocked(c: Ultimate64Client) -> Ultimate64Client:
    c._temp_hygiene_blocked = "FTP File Service unreachable (test)"
    return c


def test_refusal_message_on_a_never_probed_client_issues_no_http():
    """The reachable case from #265: ``temp_hygiene=True`` arms a client whose
    threshold was pinned, so it has never probed; the refusal must not be the
    first request it makes."""
    mock, seen = _wire()
    c = _blocked(_pinned(temp_hygiene=True))
    assert c.temp_hygiene_armed is True
    with patch("urllib.request.urlopen", mock), \
            pytest.raises(Ultimate64TempHygieneError) as ei:
        c._refuse_or_warn("POST /v1/machine:writemem")
    assert seen == [], [r.get_full_url() for r in seen]
    assert c._capabilities is None
    assert "(unknown)" in str(ei.value)


def test_refusal_message_names_the_cached_firmware():
    """Positive control: with a grade cached, the version is still reported,
    so the fallback above is not simply what the message always says."""
    mock, seen = _wire()
    c = _blocked(_pinned(temp_hygiene=True))
    c._capabilities = LEAKY
    with patch("urllib.request.urlopen", mock), \
            pytest.raises(Ultimate64TempHygieneError) as ei:
        c._refuse_or_warn("POST /v1/machine:writemem")
    assert seen == []
    assert "(1.1.0)" in str(ei.value)


def test_refusal_through_a_request_on_a_never_probed_client_issues_only_nothing():
    """End to end: an attachment-creating request on that client is refused
    without a single request on the wire -- neither the POST nor a probe."""
    mock, seen = _wire()
    c = _blocked(_pinned(temp_hygiene=True))
    with patch("urllib.request.urlopen", mock), \
            pytest.raises(Ultimate64TempHygieneError):
        c.write_mem(0x4000, bytes(200))
    assert seen == [], [r.get_full_url() for r in seen]


# --------------------------------------------------------------------------- #
# #291: the transport uses the public accessor                                #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("public, private, expected", [
    (True, False, True),
    (False, True, False),
    (None, True, False),
])
def test_transport_post_safety_reads_the_public_accessor(public, private, expected):
    client = MagicMock()
    client.host = "h"
    client.password = None
    client.cached_capabilities = (
        None if public is None else MagicMock(writemem_post_safe=public)
    )
    client._capabilities = MagicMock(writemem_post_safe=private)
    t = Ultimate64Transport(host="h", client=client)
    assert t._rest_write_is_post_safe() is expected


def test_transport_does_not_touch_the_probing_property():
    client = MagicMock()
    client.host = "h"
    client.password = None
    client.cached_capabilities = MagicMock(writemem_post_safe=True)
    type(client).capabilities = property(
        lambda self: pytest.fail("transport read the probing capabilities property")
    )
    t = Ultimate64Transport(host="h", client=client)
    assert t._rest_write_is_post_safe() is True


# --------------------------------------------------------------------------- #
# Nothing outside the client reads the private attribute                      #
# --------------------------------------------------------------------------- #

_PRIVATE_READ = re.compile(r"""\._capabilities\b|["']_capabilities["']""")


def _private_readers(text: str) -> list[str]:
    return [line for line in text.splitlines() if _PRIVATE_READ.search(line)]


def test_no_module_outside_the_client_reads_private_capabilities():
    offenders = {}
    for path in sorted(_SRC.rglob("*.py")):
        if path.name == "ultimate64_client.py":
            continue
        hits = _private_readers(path.read_text())
        if hits:
            offenders[str(path.relative_to(_SRC))] = hits
    assert offenders == {}


@pytest.mark.parametrize("line", [
    'caps = getattr(self._client, "_capabilities", None)',
    "caps = self._client._capabilities",
    "x = client._capabilities.writemem_post_safe",
])
def test_private_reader_scan_fires(line):
    """Vacuity guard: the scan matches each shape the transport has used."""
    assert _private_readers(line) == [line]


@pytest.mark.parametrize("line", [
    "caps = self._client.cached_capabilities",
    "from .u64_capabilities import DeviceCapabilities",
    "self._probe_capabilities()",
])
def test_private_reader_scan_ignores_the_public_names(line):
    assert _private_readers(line) == []


def test_the_scan_walks_real_files():
    """Vacuity guard: the walk reaches the transport and the client."""
    names = {p.name for p in _SRC.rglob("*.py")}
    assert {"ultimate64.py", "ultimate64_client.py"} <= names


# --------------------------------------------------------------------------- #
# #343: inside the client, only the two accessors read the private cache      #
# --------------------------------------------------------------------------- #

import ast  # noqa: E402

#: The only functions allowed to *read* ``_capabilities``: the probing
#: property and the non-probing accessor.  Stores (``__init__``, the probe,
#: the re-probe) are not reads and are not restricted here.
_READERS_ALLOWED = {"capabilities", "cached_capabilities"}


def _private_reads_by_function(source: str) -> dict[str, int]:
    """Count reads of ``_capabilities`` per enclosing function name.

    A read is ``<expr>._capabilities`` in a load context, or the string
    ``"_capabilities"`` passed to ``getattr``/``hasattr``.  AST-based so
    docstrings and comments naming the attribute do not count.
    """
    counts: dict[str, int] = {}

    class _Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.stack: list[str] = []

        def _func(self, node):
            self.stack.append(node.name)
            self.generic_visit(node)
            self.stack.pop()

        visit_FunctionDef = _func
        visit_AsyncFunctionDef = _func

        def _hit(self) -> None:
            name = self.stack[-1] if self.stack else "<module>"
            counts[name] = counts.get(name, 0) + 1

        def visit_Attribute(self, node):
            if node.attr == "_capabilities" and isinstance(node.ctx, ast.Load):
                self._hit()
            self.generic_visit(node)

        def visit_Call(self, node):
            if (isinstance(node.func, ast.Name) and node.func.id in ("getattr", "hasattr")
                    and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == "_capabilities"):
                self._hit()
            self.generic_visit(node)

    _Visitor().visit(ast.parse(source))
    return counts


def test_only_the_accessors_read_private_capabilities_inside_the_client():
    source = (_SRC / "backends" / "ultimate64_client.py").read_text()
    reads = _private_reads_by_function(source)
    offenders = {k: v for k, v in reads.items() if k not in _READERS_ALLOWED}
    assert offenders == {}, offenders


def test_the_in_client_scan_sees_the_accessors():
    """Vacuity guard: the real accessors are found, so an empty result above
    is not a scan that matches nothing."""
    source = (_SRC / "backends" / "ultimate64_client.py").read_text()
    reads = _private_reads_by_function(source)
    assert reads.get("capabilities", 0) >= 1
    assert reads.get("cached_capabilities", 0) >= 1


@pytest.mark.parametrize("body", [
    "caps = getattr(self, '_capabilities', None)",
    "caps = self._capabilities",
    "return self._capabilities.writemem_post_safe",
])
def test_the_in_client_scan_fires_on_a_planted_reader(body):
    planted = f"class C:\n    def _effective_poked_threshold(self):\n        {body}\n"
    assert _private_reads_by_function(planted) == {"_effective_poked_threshold": 1}


@pytest.mark.parametrize("body", [
    "self._capabilities = None",
    "caps = self.cached_capabilities",
    '"""Reads the private ``_capabilities`` cache."""',
])
def test_the_in_client_scan_ignores_stores_accessors_and_prose(body):
    planted = f"class C:\n    def helper(self):\n        {body}\n"
    assert _private_reads_by_function(planted) == {}
