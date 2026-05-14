"""Tests for the refactored MemoryArbiter — a helper on top of MemoryPolicy.

The arbiter no longer owns the safety story (that's the transport-level
policy).  These tests confirm it produces addresses that ``policy.check_write``
will accept, and that it doesn't hand the same address out twice.
"""

from __future__ import annotations

import pytest

from c64_test_harness import (
    Labels,
    MemoryArbiter,
    MemoryArbiterError,
    MemoryPolicy,
    MemoryPolicyError,
    MemoryRegion,
    UnknownPolicy,
)


# ---------------------------------------------------------------------------
# Basics — empty policy → window-only allocation
# ---------------------------------------------------------------------------


class TestBasics:
    def test_default_returns_window_start(self) -> None:
        a = MemoryArbiter()
        # Default window starts at $0200.
        assert a.alloc(16, name="first") == 0x0200

    def test_sequential_allocs_dont_overlap(self) -> None:
        a = MemoryArbiter()
        first = a.alloc(256, name="a")
        second = a.alloc(256, name="b")
        assert first == 0x0200
        assert second == 0x0300
        assert first + 256 <= second

    def test_alignment(self) -> None:
        a = MemoryArbiter()
        a.alloc(17, name="prefix")
        aligned = a.alloc(256, alignment=256, name="aligned")
        assert aligned == 0x0300

    def test_invalid_alignment_rejected(self) -> None:
        a = MemoryArbiter()
        with pytest.raises(ValueError, match="power of two"):
            a.alloc(16, alignment=3, name="bad")

    def test_invalid_size_rejected(self) -> None:
        a = MemoryArbiter()
        with pytest.raises(ValueError, match="size must be >= 1"):
            a.alloc(0, name="empty")


# ---------------------------------------------------------------------------
# Reserved regions skipped
# ---------------------------------------------------------------------------


class TestReserved:
    def test_alloc_skips_reserved_range(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x0200, 0x0300, "BASIC_TMP"),),
        )
        a = MemoryArbiter(policy=policy)
        assert a.alloc(16, name="stub") == 0x0300

    def test_alloc_fits_in_gap_between_two_reserves(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(
                MemoryRegion(0x0200, 0x0300, "low"),
                MemoryRegion(0x0400, 0x0500, "high"),
            ),
        )
        a = MemoryArbiter(policy=policy)
        assert a.alloc(256, name="middle") == 0x0300

    def test_alloc_overflows_gap_into_next_free(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(
                MemoryRegion(0x0200, 0x0300, "low"),
                MemoryRegion(0x0400, 0x0500, "high"),
            ),
        )
        a = MemoryArbiter(policy=policy)
        # Request larger than the $0300-$03FF gap — lands past second reserve.
        assert a.alloc(512, name="big") == 0x0500


# ---------------------------------------------------------------------------
# Safe regions restrict the search
# ---------------------------------------------------------------------------


class TestSafeRegions:
    def test_alloc_restricted_to_safe(self) -> None:
        policy = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000, "scratch"),),
            unknown=UnknownPolicy.DENY,
        )
        a = MemoryArbiter(policy=policy)
        addr = a.alloc(16, name="stub")
        assert 0xC000 <= addr < 0xD000

    def test_alloc_exhausts_safe_and_fails(self) -> None:
        policy = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xC010, "tiny"),),
            unknown=UnknownPolicy.DENY,
        )
        a = MemoryArbiter(policy=policy)
        assert a.alloc(16, name="first") == 0xC000
        with pytest.raises(MemoryArbiterError):
            a.alloc(1, name="second")  # safe region full

    def test_alloc_with_multiple_safe_regions(self) -> None:
        policy = MemoryPolicy(
            safe_regions=(
                MemoryRegion(0x0334, 0x0400, "cassette scratch"),
                MemoryRegion(0xC000, 0xD000, "high scratch"),
            ),
        )
        a = MemoryArbiter(policy=policy)
        # First fit is the cassette scratch region.
        assert a.alloc(16, name="stub") == 0x0334


# ---------------------------------------------------------------------------
# Window constraints
# ---------------------------------------------------------------------------


class TestWindow:
    def test_custom_window_clips_lower_bound(self) -> None:
        a = MemoryArbiter(window=(0x0800, 0xFFFF))
        assert a.alloc(16, name="stub") == 0x0800

    def test_request_too_big_for_window_raises(self) -> None:
        a = MemoryArbiter(window=(0xFF00, 0xFF7F))  # 0x80 bytes available
        with pytest.raises(MemoryArbiterError):
            a.alloc(0x100, name="overflow")


# ---------------------------------------------------------------------------
# Labels integration
# ---------------------------------------------------------------------------


class TestFromLabels:
    def test_labels_addresses_become_reserved(self) -> None:
        labels = Labels()
        labels._by_name["x25_basepoint"] = 0x4200
        labels._by_addr[0x4200] = "x25_basepoint"
        a = MemoryArbiter.from_labels(labels, window=(0x4200, 0x42FF))
        # The single byte at $4200 is reserved → first fit is $4201.
        assert a.alloc(16, name="stub") == 0x4201

    def test_label_filter_skips_code_symbols(self) -> None:
        labels = Labels()
        labels._by_name["code_entry"] = 0x4200
        labels._by_addr[0x4200] = "code_entry"
        a = MemoryArbiter.from_labels(
            labels,
            label_address_is_data=lambda n: not n.startswith("code_"),
            window=(0x4200, 0x42FF),
        )
        assert a.alloc(16, name="stub") == 0x4200

    def test_extra_reserved_layered_on_labels(self) -> None:
        labels = Labels()
        a = MemoryArbiter.from_labels(
            labels,
            extra_reserved=(MemoryRegion(0x0200, 0x0300, "extra"),),
        )
        assert a.alloc(16, name="stub") == 0x0300


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------


class TestBookkeeping:
    def test_allocations_property(self) -> None:
        a = MemoryArbiter()
        a.alloc(16, name="first")
        a.alloc(16, name="second")
        names = [name for _, _, name in a.allocations]
        assert names == ["first", "second"]
        # Inclusive end form per the docstring.
        start, end_incl, _ = a.allocations[0]
        assert start == 0x0200
        assert end_incl == 0x020F

    def test_reserve_marks_range_taken(self) -> None:
        a = MemoryArbiter()
        a.reserve(MemoryRegion(0x0200, 0x0300, "manual"))
        assert a.alloc(16, name="stub") == 0x0300


# ---------------------------------------------------------------------------
# policy_with_allocations — derive a stricter policy for the transport
# ---------------------------------------------------------------------------


class TestPolicyWithAllocations:
    def test_derived_policy_blocks_subsequent_writes(self) -> None:
        policy = MemoryPolicy.permissive()
        a = MemoryArbiter(policy=policy)
        scratch = a.alloc(16, name="scratch")
        derived = a.policy_with_allocations()
        # The derived policy must block writes to the just-allocated span.
        with pytest.raises(MemoryPolicyError):
            derived.check_write(scratch, 16)
        # And it remains permissive elsewhere (window-policy was permissive).
        derived.check_write(0xC000, 16)


# ---------------------------------------------------------------------------
# Diagnostics — regression for issue #93 / c64-https Phase C.5
# ---------------------------------------------------------------------------


class TestIssue93Regression:
    """The c64-https Phase C.5 collision: caller hardcoded ``$4200`` as
    a trampoline target, the new X25519 library moved RODATA+BSS into
    ``$4200-$50FF``, the two writes overlapped, lookup tables were
    silently clobbered, 12 hours of bisection followed.

    With the arbiter holding the consumer's full layout, the alloc
    fails loudly at test-launch time and the trace identifies the
    colliding region.
    """

    def test_x25519_alloc_returns_clean_address(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(
                MemoryRegion.parse("$0801-$1FFF", note="LOADER"),
                MemoryRegion.parse("$2000-$3FFF", note="NET_CODE"),
                MemoryRegion.parse("$4000-$50FF", note="X25519 RODATA + BSS"),
                MemoryRegion.parse("$5100-$5FFF", note="NET_BSS"),
                MemoryRegion.parse("$6000-$9FFF", note="CRYPTO"),
                MemoryRegion.parse("$A000-$BFFF", note="SHADOW_BSS"),
                MemoryRegion.parse("$C000-$CFFF", note="TCP_BUF"),
            ),
        )
        a = MemoryArbiter(policy=policy)
        addr = a.alloc(117, name="trampoline")
        # Whatever address it returned must pass the same policy.
        policy.check_write(addr, 117)

    def test_constrained_window_surfaces_collision_loudly(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(
                MemoryRegion.parse("$4000-$50FF", note="X25519 RODATA + BSS"),
            ),
        )
        a = MemoryArbiter(policy=policy, window=(0x4200, 0x4542))
        with pytest.raises(MemoryArbiterError) as ei:
            a.alloc(117, name="trampoline")
        # No free intervals exist inside the reserved region, so the
        # trace is empty but the exception message names the failure.
        assert "no free range" in str(ei.value)
        assert "trampoline" in str(ei.value)
