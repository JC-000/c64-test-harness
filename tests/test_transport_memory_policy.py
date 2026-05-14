"""Transport-boundary tests for MemoryPolicy enforcement.

Both BinaryViceTransport.write_memory and Ultimate64Transport.write_memory
must consult ``self.memory_policy`` before issuing the wire write.  The
unit tests in ``test_memory_policy.py`` cover the policy's
``check_write`` logic on its own; this file confirms the wiring exists
inside both transports and that the override kwarg works through the
public API.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from c64_test_harness import (
    MemoryPolicy,
    MemoryPolicyError,
    MemoryRegion,
    UnknownPolicy,
)
from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.backends.vice_binary import BinaryViceTransport


# ---------------------------------------------------------------------------
# VICE transport wiring — _connect is patched out, _send_and_recv stubbed
# ---------------------------------------------------------------------------


def _make_vice_transport(policy: MemoryPolicy | None = None) -> BinaryViceTransport:
    """Construct a BinaryViceTransport with the connection step suppressed."""
    with patch.object(BinaryViceTransport, "_connect"):
        return BinaryViceTransport(memory_policy=policy)


class TestViceTransportPolicy:
    def test_default_is_permissive(self) -> None:
        t = _make_vice_transport()
        assert t.memory_policy.is_permissive()

    def test_constructor_accepts_policy(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x4200, 0x5100, "X25519"),),
        )
        t = _make_vice_transport(policy=policy)
        assert t.memory_policy is policy

    def test_setter_swaps_policy(self) -> None:
        t = _make_vice_transport()
        policy = MemoryPolicy(unknown=UnknownPolicy.DENY)
        t.memory_policy = policy
        assert t.memory_policy is policy

    def test_setter_rejects_non_policy(self) -> None:
        t = _make_vice_transport()
        with pytest.raises(TypeError, match="MemoryPolicy"):
            t.memory_policy = "not a policy"  # type: ignore[assignment]

    def test_write_into_reserved_raises_before_wire(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x4200, 0x5100, "X25519"),),
        )
        t = _make_vice_transport(policy=policy)
        with patch.object(t, "_send_and_recv") as send:
            with pytest.raises(MemoryPolicyError):
                t.write_memory(0x4200, b"\xAA\xBB")
            send.assert_not_called()  # crucially: no byte crossed the wire

    def test_write_with_override_proceeds(self, caplog: pytest.LogCaptureFixture) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x4200, 0x5100, "X25519"),),
        )
        t = _make_vice_transport(policy=policy)
        with patch.object(t, "_send_and_recv") as send:
            with caplog.at_level("WARNING"):
                t.write_memory(0x4200, b"\xAA", override="testing override")
            send.assert_called_once()
        assert any("memory policy override" in r.message for r in caplog.records)

    def test_permissive_writes_proceed_silently(self) -> None:
        t = _make_vice_transport()
        with patch.object(t, "_send_and_recv") as send:
            t.write_memory(0x4200, b"\xAA")
            send.assert_called_once()


# ---------------------------------------------------------------------------
# U64 transport wiring — client.write_mem is the wire side
# ---------------------------------------------------------------------------


@pytest.fixture
def u64_client() -> MagicMock:
    client = MagicMock()
    client.read_mem.return_value = b""
    return client


class TestU64TransportPolicy:
    def test_default_is_permissive(self, u64_client: MagicMock) -> None:
        t = Ultimate64Transport(host="h", client=u64_client)
        assert t.memory_policy.is_permissive()

    def test_constructor_accepts_policy(self, u64_client: MagicMock) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0xC000, 0xD000, "TCP_BUF"),),
        )
        t = Ultimate64Transport(host="h", client=u64_client, memory_policy=policy)
        assert t.memory_policy is policy

    def test_setter_swaps_policy(self, u64_client: MagicMock) -> None:
        t = Ultimate64Transport(host="h", client=u64_client)
        policy = MemoryPolicy(unknown=UnknownPolicy.DENY)
        t.memory_policy = policy
        assert t.memory_policy is policy

    def test_setter_rejects_non_policy(self, u64_client: MagicMock) -> None:
        t = Ultimate64Transport(host="h", client=u64_client)
        with pytest.raises(TypeError, match="MemoryPolicy"):
            t.memory_policy = 42  # type: ignore[assignment]

    def test_write_into_reserved_raises_before_wire(self, u64_client: MagicMock) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0xC000, 0xD000, "TCP_BUF"),),
        )
        t = Ultimate64Transport(host="h", client=u64_client, memory_policy=policy)
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0xC000, b"\xAA\xBB")
        u64_client.write_mem.assert_not_called()

    def test_write_with_override_proceeds(
        self, u64_client: MagicMock, caplog: pytest.LogCaptureFixture
    ) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0xC000, 0xD000, "TCP_BUF"),),
        )
        t = Ultimate64Transport(host="h", client=u64_client, memory_policy=policy)
        with caplog.at_level("WARNING"):
            t.write_memory(0xC000, b"\xAA", override="known clobber")
        u64_client.write_mem.assert_called_once_with(0xC000, b"\xAA")
        assert any("memory policy override" in r.message for r in caplog.records)

    def test_permissive_writes_proceed_silently(self, u64_client: MagicMock) -> None:
        t = Ultimate64Transport(host="h", client=u64_client)
        t.write_memory(0xC000, b"\xAA")
        u64_client.write_mem.assert_called_once_with(0xC000, b"\xAA")

    def test_safe_region_with_deny_unknown_admits_in_range(
        self, u64_client: MagicMock
    ) -> None:
        policy = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000, "scratch"),),
            unknown=UnknownPolicy.DENY,
        )
        t = Ultimate64Transport(host="h", client=u64_client, memory_policy=policy)
        t.write_memory(0xC100, b"\xAA")  # inside safe
        u64_client.write_mem.assert_called_once()
        with pytest.raises(MemoryPolicyError):
            t.write_memory(0x0200, b"\xAA")  # outside safe → unknown → deny
