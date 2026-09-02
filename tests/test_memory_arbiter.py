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
        # Raw mechanics test: $0200-$03FF crosses the keyboard buffer and
        # cassette-buffer scratch, so opt out of the #169 exclusion.
        a = MemoryArbiter(exclude_harness_scratch=False)
        first = a.alloc(256, name="a")
        second = a.alloc(256, name="b")
        assert first == 0x0200
        assert second == 0x0300
        assert first + 256 <= second

    def test_alignment(self) -> None:
        # Raw mechanics test (see above) — $0300 page holds CINV/$0334.
        a = MemoryArbiter(exclude_harness_scratch=False)
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
        # Raw mechanics test: the $0300 gap holds harness scratch (CINV,
        # jsr trampoline, run_subroutine flags) — opt out of #169.
        a = MemoryArbiter(policy=policy, exclude_harness_scratch=False)
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
        # Raw mechanics test: $C000 is the UCI/SID stub page — opt out.
        a = MemoryArbiter(policy=policy, exclude_harness_scratch=False)
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
        # Raw mechanics test: both safe regions are harness scratch, and
        # the point here is first-fit ordering — opt out of #169.
        a = MemoryArbiter(policy=policy, exclude_harness_scratch=False)
        # First fit is the cassette scratch region.
        assert a.alloc(16, name="stub") == 0x0334
        # With the default exclusion the same policy skips $0334-$0341
        # (jsr trampoline, SID park JMP, SID song trampoline) and lands
        # on the first free byte after them.
        assert MemoryArbiter(policy=policy).alloc(8, name="stub") == 0x0342


# ---------------------------------------------------------------------------
# Allocations must pass check_write — reserved-only policy + unknown
# ---------------------------------------------------------------------------


class TestAllocPassesCheckWrite:
    def test_reserved_only_deny_raises_descriptive_error(self) -> None:
        # With no safe_regions and unknown=DENY, every "free" interval
        # is still unknown territory that check_write refuses.  The old
        # code returned an address whose write then raised
        # MemoryPolicyError — breaking the "guaranteed to pass
        # check_write" promise.
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x0200, 0x0300, "BASIC_TMP"),),
            unknown=UnknownPolicy.DENY,
        )
        a = MemoryArbiter(policy=policy)
        with pytest.raises(MemoryArbiterError) as ei:
            a.alloc(16, name="stub")
        msg = str(ei.value)
        assert "denies unknown addresses" in msg
        assert "no safe_regions" in msg

    def test_reserved_only_warn_alloc_passes_check_write(self) -> None:
        # WARN shape: the address is handed out (check_write passes,
        # warning or not) and the promise holds.
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x0200, 0x0300, "BASIC_TMP"),),
            unknown=UnknownPolicy.WARN,
        )
        a = MemoryArbiter(policy=policy)
        import warnings as _warnings

        with _warnings.catch_warnings():
            _warnings.simplefilter("error")  # alloc itself must not warn
            addr = a.alloc(16, name="stub")
        assert addr == 0x0300
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore")
            policy.check_write(addr, 16)  # does not raise

    def test_deny_with_safe_regions_still_allocates(self) -> None:
        # DENY is fine when safe_regions exist — allocation is
        # restricted to them and check_write passes.
        policy = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000, "scratch"),),
            reserved_regions=(MemoryRegion(0x0200, 0x0300, "BASIC_TMP"),),
            unknown=UnknownPolicy.DENY,
        )
        a = MemoryArbiter(policy=policy)
        addr = a.alloc(16, name="stub")
        policy.check_write(addr, 16)  # does not raise


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


# ---------------------------------------------------------------------------
# Issue #169 — the arbiter must never hand out the harness's own scratch
# ---------------------------------------------------------------------------


class TestHarnessScratchDefault:
    """The issue's measurement: ``MemoryArbiter(MemoryPolicy.permissive())``
    reported every documented harness scratch address as free to allocate.
    It must not — the allocator whose job is safe addresses was handing
    back the exact byte ``jsr()`` writes its trampoline to.
    """

    ISSUE_ADDRESSES = [
        0x0277, 0x0334, 0x0339, 0x0360, 0x03F0,
        0xC000, 0xC400, 0xC500, 0xCF00,
    ]

    @pytest.mark.parametrize("addr", ISSUE_ADDRESSES)
    def test_documented_scratch_is_not_free(self, addr: int) -> None:
        a = MemoryArbiter(MemoryPolicy.permissive())
        assert a.is_free(addr) is False, f"${addr:04X} reported free"

    @pytest.mark.parametrize("addr", ISSUE_ADDRESSES)
    def test_alloc_pinned_to_scratch_address_refuses(self, addr: int) -> None:
        a = MemoryArbiter(MemoryPolicy.permissive(), window=(addr, addr))
        with pytest.raises(MemoryArbiterError) as ei:
            a.alloc(1, name="pinned")
        assert "harness scratch" in str(ei.value)

    def test_allocations_never_overlap_harness_scratch(self) -> None:
        from c64_test_harness import harness_scratch_regions

        a = MemoryArbiter(MemoryPolicy.permissive(), window=(0x0200, 0xCFFF))
        scratch = harness_scratch_regions()
        for i in range(64):
            base = a.alloc(16, name=f"blk{i}")
            for r in scratch:
                assert not r.overlaps_range(base, 16), (hex(base), str(r))

    def test_is_free_positive_case(self) -> None:
        a = MemoryArbiter(MemoryPolicy.permissive())
        assert a.is_free(0x0200) is True
        assert a.is_free(0x0200, 0x77) is True   # $0200-$0276, stops at KEYD
        assert a.is_free(0x0200, 0x78) is False  # ... touches $0277

    def test_is_free_respects_reserved_and_allocations(self) -> None:
        policy = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x4000, 0x5000, "consumer"),),
        )
        a = MemoryArbiter(policy)
        assert a.is_free(0x4000) is False
        taken = a.alloc(16, name="x")
        assert a.is_free(taken) is False

    def test_transient_scratch_is_not_withheld(self) -> None:
        # The 32 KiB REU staging window is saved/restored around its use;
        # it is declared, but allocation there stays allowed by default.
        a = MemoryArbiter(MemoryPolicy.permissive())
        assert a.is_free(0x0800) is True

    def test_opt_out_restores_raw_allocation(self) -> None:
        a = MemoryArbiter(MemoryPolicy.permissive(), exclude_harness_scratch=False)
        assert a.is_free(0x0334) is True
        assert a.alloc(5, name="own trampoline") == 0x0200  # window start

    def test_from_labels_honours_opt_out(self) -> None:
        labels = Labels()
        strict = MemoryArbiter.from_labels(labels, window=(0x0334, 0x0338))
        with pytest.raises(MemoryArbiterError):
            strict.alloc(5, name="stub")
        raw = MemoryArbiter.from_labels(
            labels, window=(0x0334, 0x0338), exclude_harness_scratch=False,
        )
        assert raw.alloc(5, name="stub") == 0x0334

    def test_default_first_fit_skips_cassette_buffer_scratch(self) -> None:
        # A 256-byte request used to land on $0200-$02FF straight across
        # the keyboard buffer at $0277.  Now the first span that clears
        # every non-transient scratch entry is after the U64 flags.
        a = MemoryArbiter(MemoryPolicy.permissive())
        assert a.alloc(256, name="page") == 0x03F2

    @pytest.mark.parametrize("addr", [0xC300, 0xC3FE])
    def test_uci_block_interior_is_not_free(self, addr: int) -> None:
        # The UCI status page and completion sentinel sit mid-block; a
        # transient=True slip on the $C000-$C3FF entry would free them.
        assert MemoryArbiter().is_free(addr) is False
