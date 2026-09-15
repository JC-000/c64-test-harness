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
        (128, "unprobed", [128, 128, 44]),  # unknown grade: explicit threshold
        (128, None, [128, 128, 44]),  # unknown grade: probe failed
        (48, POST_SAFE, [48] * 6 + [12]),
    ],
    ids=["leak-prone", "unknown-unprobed", "unknown-probe-failed", "post-safe"],
)
def test_write_bytes_never_posts_on_any_grade(threshold, info, expected) -> None:
    """#247's hazard: at a fixed 84 a 48-threshold device took POST chunks.

    Owner ruling on #252 (2026-09-15): ``write_bytes`` derives its chunk from
    the transport's ``rest_put_chunk_size``, so every chunk is a PUT on every
    grade, accepting more, smaller requests on a post-safe device.
    """
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


def test_write_bytes_ignores_a_non_positive_reported_chunk_size() -> None:
    """``_write_chunk_size``'s ``size > 0`` guard (review N3): a transport
    reporting 0 must not reach ``range(step=0)``; it keeps the 84 default."""

    class _ZeroReporter:
        rest_put_chunk_size = 0

        def __init__(self) -> None:
            self.calls: list[int] = []

        def write_memory(self, addr, data):
            self.calls.append(len(data))

    t = _ZeroReporter()
    write_bytes(t, 0x1000, _payload(100))
    assert t.calls == [84, 16]


# --------------------------------------------------------------------------- #
# Review round 1                                                               #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("info,threshold", [(LEAK_PRONE, 128), (POST_SAFE, 48)])
def test_write_crossing_ffff_is_refused_before_any_request(info, threshold) -> None:
    """Finding 1: chunking must not half-write a span the firmware refuses whole."""
    client, wire = _client(threshold, info)
    with pytest.raises(ValueError, match=r"\$FFFF"):
        _transport(client).write_memory(0xFF00, _payload(300))
    assert wire.requests == []


@pytest.mark.parametrize("info,threshold", [(LEAK_PRONE, 128), (POST_SAFE, 48)])
def test_write_crossing_ffff_is_refused_before_the_policy_check(info, threshold) -> None:
    client, wire = _client(threshold, info)
    policy = MagicMock(spec=MemoryPolicy)
    policy.is_permissive.return_value = False
    t = _transport(client)
    t._memory_policy = policy
    with pytest.raises(ValueError):
        t.write_memory(0xFFFF, _payload(2))
    policy.check_write.assert_not_called()
    assert wire.requests == []


@pytest.mark.parametrize("info,threshold", [(LEAK_PRONE, 128), (POST_SAFE, 48)])
def test_write_ending_exactly_at_ffff_is_allowed(info, threshold) -> None:
    client, wire = _client(threshold, info)
    data = _payload(256)  # $FF00 + 256 == 0x10000
    _transport(client).write_memory(0xFF00, data)
    assert wire.reassemble(0xFF00) == data


def test_negative_address_is_refused_before_any_request() -> None:
    client, wire = _client(128, LEAK_PRONE)
    # The transport's own whole-span message, not the client's start check.
    with pytest.raises(ValueError, match=r"outside \$0000-\$FFFF"):
        _transport(client).write_memory(-1, _payload(4))
    assert wire.requests == []


def test_zero_threshold_post_safe_both_entry_points_send() -> None:
    """Finding 4: at threshold 0 there is no PUT-sized chunk.  A post-safe
    device needs none, so neither entry point refuses."""
    client, wire = _client(48, POST_SAFE)
    client.write_mem_query_threshold = 0
    t = _transport(client)
    assert t.rest_put_chunk_size is None
    data = _payload(100)
    t.write_memory(0x4000, data)
    assert wire.requests == [("POST", 0x4000, data)]
    wire.requests.clear()
    write_bytes(t, 0x4000, data)
    assert wire.methods == ["POST", "POST"]  # the 84-byte default, collected
    assert wire.reassemble(0x4000) == data


def test_zero_threshold_leak_prone_both_entry_points_refuse() -> None:
    client, wire = _client(128, LEAK_PRONE)
    client.write_mem_query_threshold = 0
    t = _transport(client)
    with pytest.raises(ValueError, match="positive"):
        t.write_memory(0x4000, _payload(100))
    with pytest.raises(ValueError, match="positive"):
        write_bytes(t, 0x4000, _payload(100))
    assert wire.requests == []


def test_client_without_integer_threshold_chunks_at_the_put_cap() -> None:
    """Review N1: a test double with no integer threshold gets 128, not 84."""
    client = MagicMock()
    client.cached_capabilities.writemem_post_safe = False
    t = Ultimate64Transport(host="h", client=client)
    assert t.rest_put_chunk_size == 128
    t.write_memory(0x4000, _payload(300))
    assert [len(c.args[1]) for c in client.write_mem.call_args_list] == [128, 128, 44]
