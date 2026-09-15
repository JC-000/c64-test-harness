"""#252 option C: ``Ultimate64Transport.write_memory`` chunks on a device that
is not graded post-safe, and ``memory.write_bytes`` derives its chunk size
from the client threshold.

No device traffic: a real :class:`Ultimate64Client` is constructed with an
explicit threshold (which issues no HTTP by contract) and its ``_request`` is
replaced by a recorder, so what is asserted is exactly what would reach the
wire -- method, address, and body/data -- without a socket ever opening.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.u64_capabilities import DeviceCapabilities
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.ultimate64_client import Ultimate64Client
from c64_test_harness.memory import write_bytes
from c64_test_harness.memory_policy import (
    MemoryPolicy,
    MemoryPolicyError,
    MemoryRegion,
    UnknownPolicy,
)

LEAK_PRONE = {"firmware_version": "1.1.0", "product": "C64 Ultimate"}
POST_SAFE = {"firmware_version": "3.15", "product": "Ultimate 64 Elite"}


def _payload(n: int) -> bytes:
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


class _Wire:
    """Records every request ``write_mem`` would have sent."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, int, bytes]] = []

    def __call__(self, method, path, *, body=None, content_type=None, query=None):
        assert path == "/v1/machine:writemem", path
        addr = int(query["address"], 16)
        if method == "PUT":
            assert body is None
            data = bytes.fromhex(query["data"])
        else:
            assert method == "POST" and "data" not in query
            data = body
        self.requests.append((method, addr, data))
        return 200, b""

    @property
    def methods(self) -> list[str]:
        return [m for m, _, _ in self.requests]

    def reassemble(self, base: int) -> bytes:
        out = bytearray()
        for _, addr, data in self.requests:
            assert addr == base + len(out), "chunks must be contiguous and in order"
            out += data
        return bytes(out)


def _client(threshold: int, info: dict | None = "unprobed") -> tuple[Ultimate64Client, _Wire]:
    client = Ultimate64Client(
        "192.0.2.1", write_mem_query_threshold=threshold, warn_unlocked=False
    )
    if info != "unprobed":
        # What a construct-time probe would have cached.
        client._capabilities = DeviceCapabilities.from_info(info)
    wire = _Wire()
    client._request = wire  # type: ignore[method-assign]
    return client, wire


def _transport(client: Ultimate64Client, **kw) -> Ultimate64Transport:
    return Ultimate64Transport(host=client.host, client=client, **kw)


# --------------------------------------------------------------------------- #
# Red: a > threshold write on a leak-prone grade was one leaking POST         #
# --------------------------------------------------------------------------- #

def test_leak_prone_write_over_threshold_never_posts() -> None:
    client, wire = _client(128, LEAK_PRONE)
    data = _payload(1000)
    _transport(client).write_memory(0x4000, data)
    assert "POST" not in wire.methods, wire.methods
    assert all(len(d) <= 128 for _, _, d in wire.requests)
    assert len(wire.requests) == 8  # ceil(1000 / 128)
    assert wire.reassemble(0x4000) == data


def test_leak_prone_boundary_128_is_one_put() -> None:
    client, wire = _client(128, LEAK_PRONE)
    data = _payload(128)
    _transport(client).write_memory(0x2000, data)
    assert wire.requests == [("PUT", 0x2000, data)]


def test_leak_prone_boundary_129_splits_128_plus_1() -> None:
    client, wire = _client(128, LEAK_PRONE)
    data = _payload(129)
    _transport(client).write_memory(0x2000, data)
    assert wire.requests == [
        ("PUT", 0x2000, data[:128]),
        ("PUT", 0x2080, data[128:]),
    ]


# --------------------------------------------------------------------------- #
# Paired: a post-safe grade keeps today's single request byte-for-byte        #
# --------------------------------------------------------------------------- #

def test_post_safe_write_is_exactly_one_request() -> None:
    client, wire = _client(48, POST_SAFE)
    data = _payload(1000)
    _transport(client).write_memory(0x4000, data)
    assert wire.requests == [("POST", 0x4000, data)]


def test_post_safe_small_write_is_one_put() -> None:
    client, wire = _client(48, POST_SAFE)
    data = _payload(48)
    _transport(client).write_memory(0x4000, data)
    assert wire.requests == [("PUT", 0x4000, data)]


# --------------------------------------------------------------------------- #
# Unknown grade chunks (CLAUDE.md rule 1a trap: explicit threshold, unprobed)  #
# --------------------------------------------------------------------------- #

def test_unprobed_client_chunks_and_issues_no_probe() -> None:
    client, wire = _client(128)  # explicit threshold => never probed
    assert client._capabilities is None
    data = _payload(129)
    _transport(client).write_memory(0x3000, data)
    assert wire.methods == ["PUT", "PUT"]
    # The transport must not have probed on its own (the recorder asserts
    # every request is a writemem, and the cache is still empty).
    assert client._capabilities is None
    assert wire.reassemble(0x3000) == data


def test_probe_failed_grade_chunks() -> None:
    client, wire = _client(128, None)  # from_info(None): probe failed
    assert client._capabilities.firmware_version is None
    _transport(client).write_memory(0x3000, _payload(300))
    assert "POST" not in wire.methods
    assert len(wire.requests) == 3


def test_directly_constructed_none_grade_chunks() -> None:
    """#247: ``writemem_post_safe=None`` from a hand-built fixture is unknown."""
    client, wire = _client(128)
    from dataclasses import replace

    client._capabilities = replace(
        DeviceCapabilities.from_info(POST_SAFE), writemem_post_safe=None
    )
    _transport(client).write_memory(0x3000, _payload(200))
    assert wire.methods == ["PUT", "PUT"]


def test_unprobed_chunk_size_follows_the_threshold_not_a_literal() -> None:
    client, wire = _client(48)  # unprobed, explicit 48
    data = _payload(100)
    _transport(client).write_memory(0x5000, data)
    assert [len(d) for _, _, d in wire.requests] == [48, 48, 4]
    assert wire.methods == ["PUT", "PUT", "PUT"]


def test_threshold_above_put_cap_still_chunks_at_the_cap() -> None:
    client, wire = _client(1024)  # unprobed, nonsensical explicit threshold
    _transport(client).write_memory(0x5000, _payload(300))
    assert [len(d) for _, _, d in wire.requests] == [128, 128, 44]


def test_non_positive_threshold_is_refused_before_any_request() -> None:
    client, wire = _client(128, LEAK_PRONE)
    client.write_mem_query_threshold = 0
    with pytest.raises(ValueError):
        _transport(client).write_memory(0x5000, _payload(10))
    assert wire.requests == []


# --------------------------------------------------------------------------- #
# MemoryPolicy: checked once, against the whole write, before chunking         #
# --------------------------------------------------------------------------- #

def test_policy_checks_whole_write_once_before_any_chunk() -> None:
    client, wire = _client(128, LEAK_PRONE)
    # Allow only the first chunk's span: a per-chunk check would let the
    # first chunk through before refusing the second.
    policy = MemoryPolicy(
        # Half-open: exactly the first 128-byte chunk, $6000-$607F.
        safe_regions=(MemoryRegion(0x6000, 0x6080, "first chunk only"),),
        unknown=UnknownPolicy.DENY,
    )
    t = _transport(client, memory_policy=policy)
    with pytest.raises(MemoryPolicyError):
        t.write_memory(0x6000, _payload(200))
    assert wire.requests == []


def test_policy_check_called_once_with_full_length() -> None:
    client, wire = _client(128, LEAK_PRONE)
    policy = MagicMock(spec=MemoryPolicy)
    policy.is_permissive.return_value = False
    t = _transport(client)
    t._memory_policy = policy
    t.write_memory(0x6000, _payload(300))
    policy.check_write.assert_called_once_with(0x6000, 300, override=None)
    assert len(wire.requests) == 3


# --------------------------------------------------------------------------- #
# memory.write_bytes derives its chunk from the client threshold               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "threshold,info,expected",
    [
        (128, LEAK_PRONE, [128, 128, 44]),
        (48, POST_SAFE, [48] * 6 + [12]),
    ],
)
def test_write_bytes_never_posts_on_any_grade(threshold, info, expected) -> None:
    """#247's hazard: at a fixed 84 a 48-threshold device took POST chunks."""
    client, wire = _client(threshold, info)
    data = _payload(300)
    write_bytes(_transport(client), 0x7000, data)
    assert wire.methods == ["PUT"] * len(expected)
    assert [len(d) for _, _, d in wire.requests] == expected
    assert wire.reassemble(0x7000) == data


def test_write_bytes_keeps_84_on_transports_without_a_threshold() -> None:
    """VICE transports have no REST threshold: the text-monitor 84 stands."""
    from c64_test_harness import memory

    class _ViceLike:
        def __init__(self) -> None:
            self.calls: list[tuple[int, bytes]] = []

        def write_memory(self, addr, data):
            self.calls.append((addr, bytes(data)))

    t = _ViceLike()
    write_bytes(t, 0x1000, _payload(200))
    assert [len(d) for _, d in t.calls] == [84, 84, 32]
    assert memory._WRITE_CHUNK_SIZE == 84  # importable for downstream repos
