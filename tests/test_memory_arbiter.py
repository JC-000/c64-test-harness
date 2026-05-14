"""Tests for memory_arbiter.py — first-fit DMA address allocator."""

from __future__ import annotations

import pytest

from c64_test_harness import Labels, MemoryArbiter, MemoryArbiterError


class TestMemoryArbiterBasics:
    def test_empty_arbiter_returns_window_start(self) -> None:
        arbiter = MemoryArbiter()
        # Default window starts at $0200.
        assert arbiter.alloc(16, name="stub") == 0x0200

    def test_sequential_allocs_dont_overlap(self) -> None:
        arbiter = MemoryArbiter()
        a = arbiter.alloc(256, name="first")
        b = arbiter.alloc(256, name="second")
        assert a == 0x0200
        assert b == 0x0300
        assert a + 256 <= b

    def test_alignment_is_respected(self) -> None:
        arbiter = MemoryArbiter()
        # First alloc consumes 17 bytes from $0200 — next 256-aligned
        # address is $0300.
        arbiter.alloc(17, name="prefix")
        aligned = arbiter.alloc(256, alignment=256, name="aligned")
        assert aligned == 0x0300

    def test_non_power_of_two_alignment_rejected(self) -> None:
        arbiter = MemoryArbiter()
        with pytest.raises(ValueError, match="power of two"):
            arbiter.alloc(16, alignment=3, name="bad")


class TestReservedRanges:
    def test_alloc_skips_reserved_range(self) -> None:
        arbiter = MemoryArbiter(
            reserved=[(0x0200, 0x02FF, "BASIC_TMP")],
        )
        # First free address is past the reserved block.
        assert arbiter.alloc(16, name="stub") == 0x0300

    def test_alloc_fits_between_two_reserves(self) -> None:
        arbiter = MemoryArbiter(
            reserved=[
                (0x0200, 0x02FF, "low"),
                (0x0400, 0x04FF, "high"),
            ],
        )
        # 256 B free at $0300.
        addr = arbiter.alloc(256, name="middle")
        assert addr == 0x0300

    def test_alloc_overflows_to_next_gap(self) -> None:
        arbiter = MemoryArbiter(
            reserved=[
                (0x0200, 0x02FF, "low"),
                (0x0400, 0x04FF, "high"),
            ],
        )
        # Request larger than the gap — should land past the second
        # reserve.
        addr = arbiter.alloc(512, name="big")
        assert addr == 0x0500

    def test_inverted_reserve_rejected(self) -> None:
        with pytest.raises(ValueError, match="end < start"):
            MemoryArbiter(reserved=[(0x0500, 0x0400, "backwards")])

    def test_out_of_range_reserve_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the 16-bit"):
            MemoryArbiter(reserved=[(0x10000, 0x10010, "overflow")])

    def test_reserve_method_adds_after_construction(self) -> None:
        arbiter = MemoryArbiter()
        arbiter.reserve(0x0200, 0x02FF, name="dynamic")
        assert arbiter.alloc(16, name="stub") == 0x0300


class TestLabelsIntegration:
    def test_labels_addresses_are_treated_as_reserved(self) -> None:
        labels = Labels()
        labels._by_name["x25_basepoint"] = 0x4200
        labels._by_addr[0x4200] = "x25_basepoint"
        arbiter = MemoryArbiter(labels=labels)
        # Allocator can still place around the single-byte reserve.
        # Asking for 64 B starting at $4200 should skip to $4201.
        addr = arbiter.alloc(64, name="stub", window=(0x4200, 0x42FF))
        assert addr == 0x4201

    def test_explicit_reserved_overrides_label_name(self) -> None:
        labels = Labels()
        labels._by_name["fe_p"] = 0x4220
        labels._by_addr[0x4220] = "fe_p"
        arbiter = MemoryArbiter(
            labels=labels,
            reserved=[(0x4200, 0x42FF, "X25519 RODATA")],
        )
        # Allocation in $4200-$42FF must fail loudly — both the explicit
        # reserve and the label name say "occupied".
        with pytest.raises(MemoryArbiterError) as excinfo:
            arbiter.alloc(16, name="stub", window=(0x4200, 0x42FF))
        # Trace should mention the explicit reserve (more informative
        # than the bare label name).
        joined = "\n".join(excinfo.value.trace)
        assert "X25519 RODATA" in joined

    def test_label_filter_can_ignore_code_symbols(self) -> None:
        labels = Labels()
        labels._by_name["code_entry"] = 0x4200
        labels._by_addr[0x4200] = "code_entry"
        arbiter = MemoryArbiter(
            labels=labels,
            label_address_is_data=lambda n: not n.startswith("code_"),
        )
        # With code_* filtered out, $4200 is free.
        assert arbiter.alloc(16, name="stub", window=(0x4200, 0x42FF)) == 0x4200


class TestFailureDiagnostics:
    def test_no_free_range_raises_with_trace(self) -> None:
        arbiter = MemoryArbiter(
            reserved=[(0x0200, 0xFFFF, "everything")],
        )
        with pytest.raises(MemoryArbiterError) as excinfo:
            arbiter.alloc(16, name="hopeless")
        assert excinfo.value.size == 16
        assert excinfo.value.name == "hopeless"
        # Trace should explain the collision.
        joined = "\n".join(excinfo.value.trace)
        assert "everything" in joined

    def test_request_too_big_for_window_raises(self) -> None:
        arbiter = MemoryArbiter()
        with pytest.raises(MemoryArbiterError):
            arbiter.alloc(0x100, name="overflow", window=(0xFF00, 0xFF80))


class TestAllocationsList:
    def test_allocations_recorded_in_order(self) -> None:
        arbiter = MemoryArbiter()
        arbiter.alloc(16, name="first")
        arbiter.alloc(16, name="second")
        arbiter.alloc(16, name="third")
        names = [name for _, _, name in arbiter.allocations]
        assert names == ["first", "second", "third"]


class TestRegressionDownstreamCase:
    """Regression test for harness issue #93 / c64-https Phase C.5.

    The downstream test_https_local.py was hardcoded to inject at
    $4200..$4542, which silently collided with the new X25519 sibling
    library's RODATA + BSS region $4200-$50FF.  An arbiter configured
    with that reserved range would have surfaced the collision at
    test-launch time instead of via 12 hours of cryptographic-output
    bisection.
    """

    def test_x25519_overlay_collision_caught(self) -> None:
        arbiter = MemoryArbiter(
            reserved=[
                (0x0801, 0x1FFF, "LOADER"),
                (0x2000, 0x3FFF, "NET_CODE"),
                (0x4000, 0x50FF, "X25519 RODATA + BSS"),
                (0x5100, 0x5FFF, "NET_BSS"),
                (0x6000, 0x9FFF, "CRYPTO"),
                (0xA000, 0xBFFF, "SHADOW_BSS"),
                (0xC000, 0xCFFF, "TCP_BUF"),
            ],
        )
        # Try to allocate the trampoline at the historical "free" spot.
        with pytest.raises(MemoryArbiterError) as excinfo:
            arbiter.alloc(117, name="trampoline", window=(0x4200, 0x4542))
        joined = "\n".join(excinfo.value.trace)
        assert "X25519" in joined
