"""Tests for memory_policy.py — the transport-level write guard."""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from c64_test_harness import (
    MemoryPolicy,
    MemoryPolicyError,
    MemoryRegion,
    UnknownPolicy,
)


# ---------------------------------------------------------------------------
# MemoryRegion
# ---------------------------------------------------------------------------


class TestMemoryRegion:
    def test_construct_valid(self) -> None:
        r = MemoryRegion(0x0200, 0x0400, "ok")
        assert r.start == 0x0200
        assert r.end == 0x0400
        assert r.length == 0x0200

    def test_inverted_rejected(self) -> None:
        with pytest.raises(ValueError, match="end <= start"):
            MemoryRegion(0x0400, 0x0200)

    def test_zero_length_rejected(self) -> None:
        with pytest.raises(ValueError, match="end <= start"):
            MemoryRegion(0x0200, 0x0200)

    def test_out_of_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="outside the 16-bit"):
            MemoryRegion(0x10000, 0x10010)
        with pytest.raises(ValueError, match="outside the 16-bit"):
            MemoryRegion(0x0000, 0x10001)

    def test_contains_addr_endpoints(self) -> None:
        r = MemoryRegion(0x0200, 0x0300)
        assert r.contains_addr(0x0200)
        assert r.contains_addr(0x02FF)
        assert not r.contains_addr(0x0300)  # end exclusive
        assert not r.contains_addr(0x01FF)

    def test_overlaps_range(self) -> None:
        r = MemoryRegion(0x0200, 0x0300)
        assert r.overlaps_range(0x01F0, 0x20)  # spans start
        assert r.overlaps_range(0x02F0, 0x20)  # spans end
        assert r.overlaps_range(0x0250, 0x10)  # inside
        assert r.overlaps_range(0x0100, 0x300)  # encloses
        assert not r.overlaps_range(0x0300, 0x10)  # touches end exclusive
        assert not r.overlaps_range(0x01F0, 0x10)  # touches start exclusive

    def test_parse_inclusive_range(self) -> None:
        r = MemoryRegion.parse("$4200-$50FF", note="X25519")
        assert r.start == 0x4200
        assert r.end == 0x5100  # inclusive form → exclusive end + 1
        assert r.note == "X25519"

    def test_parse_length_form(self) -> None:
        r = MemoryRegion.parse("$0334+5", note="trampoline")
        assert r.start == 0x0334
        assert r.end == 0x0339
        assert r.note == "trampoline"

    def test_parse_bare_addr_is_single_byte(self) -> None:
        r = MemoryRegion.parse("$03F0")
        assert r.start == 0x03F0
        assert r.end == 0x03F1

    def test_parse_decimal_hex(self) -> None:
        assert MemoryRegion.parse("0xC000+0x100").start == 0xC000
        assert MemoryRegion.parse("0xC000+0x100").end == 0xC100

    def test_str_uses_inclusive_form(self) -> None:
        r = MemoryRegion(0x4200, 0x5100, "X25519")
        s = str(r)
        assert "$4200" in s
        assert "$50FF" in s  # inclusive last byte
        assert "X25519" in s


# ---------------------------------------------------------------------------
# MemoryPolicy — default permissive behaviour
# ---------------------------------------------------------------------------


class TestPermissivePolicy:
    def test_default_is_permissive(self) -> None:
        p = MemoryPolicy()
        assert p.is_permissive()

    def test_explicit_permissive(self) -> None:
        assert MemoryPolicy.permissive().is_permissive()

    def test_permissive_passes_all_writes(self) -> None:
        p = MemoryPolicy.permissive()
        # No exceptions, no warnings.
        p.check_write(0x0334, 5)
        p.check_write(0xC000, 0x400)
        p.check_write(0x0000, 1)
        p.check_write(0xFFFF, 1)


# ---------------------------------------------------------------------------
# Reserved regions — deny-list
# ---------------------------------------------------------------------------


class TestReservedRegions:
    def test_write_inside_reserved_raises(self) -> None:
        p = MemoryPolicy(
            reserved_regions=(MemoryRegion(0x4200, 0x5100, "X25519"),),
        )
        with pytest.raises(MemoryPolicyError) as ei:
            p.check_write(0x4200, 0x100)
        assert ei.value.addr == 0x4200
        assert ei.value.length == 0x100
        assert ei.value.region is not None
        assert ei.value.region.note == "X25519"
        assert "X25519" in str(ei.value)

    def test_write_overlapping_reserved_edge_raises(self) -> None:
        p = MemoryPolicy(reserved_regions=(MemoryRegion(0x4200, 0x5100),))
        # Span ends at the first reserved byte.
        with pytest.raises(MemoryPolicyError):
            p.check_write(0x41F0, 0x20)  # writes 0x41F0..0x420F
        # Span starts at the last reserved byte.
        with pytest.raises(MemoryPolicyError):
            p.check_write(0x50FF, 0x10)

    def test_write_abutting_reserved_passes(self) -> None:
        # Reserved is exclusive on the end side: a write that ends at
        # the start of the reserved region or starts at its end byte
        # does not overlap.
        p = MemoryPolicy(reserved_regions=(MemoryRegion(0x4200, 0x5100),))
        p.check_write(0x41F0, 0x10)  # ends exactly at $4200
        p.check_write(0x5100, 0x10)  # starts exactly past $50FF

    def test_reserved_takes_precedence_over_safe(self) -> None:
        # A region that is both safe and reserved → reserved wins.
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0x0200, 0x1000, "safe"),),
            reserved_regions=(MemoryRegion(0x0334, 0x0400, "DO NOT TOUCH"),),
            unknown=UnknownPolicy.DENY,
        )
        with pytest.raises(MemoryPolicyError) as ei:
            p.check_write(0x0334, 0x20)
        assert "DO NOT TOUCH" in str(ei.value)


# ---------------------------------------------------------------------------
# Safe regions — allow-list
# ---------------------------------------------------------------------------


class TestSafeRegions:
    def test_write_fully_inside_safe_passes(self) -> None:
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000, "scratch"),),
            unknown=UnknownPolicy.DENY,
        )
        p.check_write(0xC000, 0x100)  # in
        p.check_write(0xCF00, 0x100)  # right up to end

    def test_write_partially_outside_safe_denied(self) -> None:
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000),),
            unknown=UnknownPolicy.DENY,
        )
        with pytest.raises(MemoryPolicyError):
            p.check_write(0xBF00, 0x200)  # extends below safe

    def test_abutting_safe_regions_cover_join(self) -> None:
        p = MemoryPolicy(
            safe_regions=(
                MemoryRegion(0xC000, 0xC800, "A"),
                MemoryRegion(0xC800, 0xD000, "B"),
            ),
            unknown=UnknownPolicy.DENY,
        )
        # Span straddles the abutment — both regions together cover it.
        p.check_write(0xC700, 0x200)

    def test_safe_with_gap_does_not_cover(self) -> None:
        p = MemoryPolicy(
            safe_regions=(
                MemoryRegion(0xC000, 0xC400, "A"),
                MemoryRegion(0xC500, 0xC900, "B"),  # gap at $C400-$C4FF
            ),
            unknown=UnknownPolicy.DENY,
        )
        with pytest.raises(MemoryPolicyError):
            p.check_write(0xC300, 0x200)


# ---------------------------------------------------------------------------
# Unknown territory — tri-state
# ---------------------------------------------------------------------------


class TestUnknownPolicy:
    def test_unknown_allow_passes(self) -> None:
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000),),
            unknown=UnknownPolicy.ALLOW,
        )
        p.check_write(0x0200, 4)  # outside safe, no reserves, allow

    def test_unknown_warn_emits_warning_and_passes(self) -> None:
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000),),
            unknown=UnknownPolicy.WARN,
        )
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            p.check_write(0x0200, 4)
        assert len(caught) == 1
        assert "0200" in str(caught[0].message)

    def test_unknown_deny_raises(self) -> None:
        p = MemoryPolicy(
            safe_regions=(MemoryRegion(0xC000, 0xD000),),
            unknown=UnknownPolicy.DENY,
        )
        with pytest.raises(MemoryPolicyError):
            p.check_write(0x0200, 4)

    def test_unknown_deny_with_no_safe_regions_is_strict(self) -> None:
        # Nothing declared safe → every write hits unknown.
        p = MemoryPolicy(unknown=UnknownPolicy.DENY)
        with pytest.raises(MemoryPolicyError) as ei:
            p.check_write(0x0200, 4)
        assert "no safe_regions declared" in str(ei.value)


# ---------------------------------------------------------------------------
# Override escape hatch
# ---------------------------------------------------------------------------


class TestOverride:
    def test_override_bypasses_reserved(self, caplog: pytest.LogCaptureFixture) -> None:
        p = MemoryPolicy(reserved_regions=(MemoryRegion(0x4200, 0x5100, "X25519"),))
        with caplog.at_level("WARNING"):
            p.check_write(0x4200, 0x100, override="I'm clobbering this on purpose")
        # No exception raised, but override is logged.
        assert any(
            "memory policy override" in rec.message and "$4200" in rec.message
            for rec in caplog.records
        )

    def test_override_bypasses_deny_unknown(self, caplog: pytest.LogCaptureFixture) -> None:
        p = MemoryPolicy(unknown=UnknownPolicy.DENY)
        with caplog.at_level("WARNING"):
            p.check_write(0x0200, 4, override="testing the bypass")
        assert any("memory policy override" in rec.message for rec in caplog.records)

    def test_empty_override_does_not_bypass(self) -> None:
        # Empty / None overrides do not count.
        p = MemoryPolicy(reserved_regions=(MemoryRegion(0x4200, 0x5100),))
        with pytest.raises(MemoryPolicyError):
            p.check_write(0x4200, 0x100, override="")
        with pytest.raises(MemoryPolicyError):
            p.check_write(0x4200, 0x100, override=None)


# ---------------------------------------------------------------------------
# Mutators
# ---------------------------------------------------------------------------


class TestMutators:
    def test_with_safe_adds(self) -> None:
        p = MemoryPolicy().with_safe(MemoryRegion(0xC000, 0xD000, "scratch"))
        assert len(p.safe_regions) == 1
        assert p.safe_regions[0].note == "scratch"

    def test_with_reserved_adds(self) -> None:
        p = MemoryPolicy().with_reserved(MemoryRegion(0x4200, 0x5100, "X25519"))
        assert len(p.reserved_regions) == 1

    def test_with_unknown_changes_setting(self) -> None:
        p = MemoryPolicy().with_unknown(UnknownPolicy.DENY)
        assert p.unknown == UnknownPolicy.DENY

    def test_frozen_dataclass_cannot_mutate(self) -> None:
        p = MemoryPolicy()
        with pytest.raises((AttributeError, TypeError)):
            p.unknown = UnknownPolicy.DENY  # type: ignore[misc]

    def test_merged_other_unknown_wins(self) -> None:
        a = MemoryPolicy(unknown=UnknownPolicy.ALLOW)
        b = MemoryPolicy(unknown=UnknownPolicy.DENY)
        assert a.merged(b).unknown == UnknownPolicy.DENY


# ---------------------------------------------------------------------------
# from_prg — PrgFile integration
# ---------------------------------------------------------------------------


class TestFromPrg:
    def test_reserves_load_span(self, tmp_path: Path) -> None:
        # Build a minimal PRG: load at $0801, 4 bytes payload.
        prg_path = tmp_path / "tiny.prg"
        prg_path.write_bytes(b"\x01\x08" + b"\xAA\xBB\xCC\xDD")
        p = MemoryPolicy.from_prg(prg_path)
        assert len(p.reserved_regions) == 1
        r = p.reserved_regions[0]
        assert r.start == 0x0801
        assert r.end == 0x0805  # 0x0801 + 4
        assert "PRG load image" in r.note

    def test_from_prg_default_unknown_is_warn(self, tmp_path: Path) -> None:
        prg_path = tmp_path / "tiny.prg"
        prg_path.write_bytes(b"\x01\x08\xAA")
        p = MemoryPolicy.from_prg(prg_path)
        assert p.unknown == UnknownPolicy.WARN

    def test_from_prg_with_extras(self, tmp_path: Path) -> None:
        prg_path = tmp_path / "tiny.prg"
        prg_path.write_bytes(b"\x01\x08\xAA")
        p = MemoryPolicy.from_prg(
            prg_path,
            unknown=UnknownPolicy.DENY,
            extra_reserved=(MemoryRegion(0x4000, 0x5000, "BSS"),),
            safe_regions=(MemoryRegion(0xC000, 0xD000, "scratch"),),
        )
        assert p.unknown == UnknownPolicy.DENY
        assert len(p.reserved_regions) == 2  # PRG + BSS
        assert len(p.safe_regions) == 1

    def test_from_prg_blocks_write_into_load_span(self, tmp_path: Path) -> None:
        prg_path = tmp_path / "tiny.prg"
        # Load at $0801, 0x100 bytes payload → covers $0801..$0900.
        prg_path.write_bytes(b"\x01\x08" + b"\x00" * 0x100)
        p = MemoryPolicy.from_prg(prg_path)
        with pytest.raises(MemoryPolicyError) as ei:
            p.check_write(0x0850, 4)
        assert "PRG load image" in str(ei.value)


# ---------------------------------------------------------------------------
# from_config — TOML/dict shape
# ---------------------------------------------------------------------------


class TestFromConfig:
    def test_basic_safe_and_reserved(self) -> None:
        data = {
            "safe_regions": [
                {"range": "$C000-$CFFF", "note": "scratch"},
            ],
            "reserved_regions": [
                {"range": "$4200-$50FF", "note": "X25519"},
            ],
            "unknown_policy": "deny",
        }
        p = MemoryPolicy.from_config(data)
        assert len(p.safe_regions) == 1
        assert p.safe_regions[0].note == "scratch"
        assert len(p.reserved_regions) == 1
        assert p.unknown == UnknownPolicy.DENY

    def test_bare_string_region(self) -> None:
        data = {"safe_regions": ["$C000-$CFFF"]}
        p = MemoryPolicy.from_config(data)
        assert p.safe_regions[0].start == 0xC000

    def test_start_end_int_form(self) -> None:
        data = {
            "reserved_regions": [
                {"start": 0x4200, "end": 0x5100, "note": "X25519"},
            ],
        }
        p = MemoryPolicy.from_config(data)
        assert p.reserved_regions[0].start == 0x4200
        assert p.reserved_regions[0].end == 0x5100

    def test_unknown_policy_default_is_allow(self) -> None:
        p = MemoryPolicy.from_config({})
        assert p.unknown == UnknownPolicy.ALLOW

    def test_unknown_policy_invalid_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown_policy"):
            MemoryPolicy.from_config({"unknown_policy": "wat"})

    def test_prg_path_auto_reserves(self, tmp_path: Path) -> None:
        prg_path = tmp_path / "build.prg"
        prg_path.write_bytes(b"\x01\x08" + b"\x00" * 0x100)
        p = MemoryPolicy.from_config({"prg": str(prg_path)})
        assert any(
            r.start == 0x0801 and r.end == 0x0901
            for r in p.reserved_regions
        )

    def test_missing_range_key_raises(self) -> None:
        with pytest.raises(ValueError, match="missing 'range'"):
            MemoryPolicy.from_config({"safe_regions": [{"note": "no addr"}]})


# ---------------------------------------------------------------------------
# Regression — Issue #93 / c64-https Phase C.5 collision shape
# ---------------------------------------------------------------------------


class TestIssue93Regression:
    """If the c64-https consumer had loaded a policy reserving its
    $4200-$50FF X25519 region, an attempt to ``write_memory($4200, ...)``
    for a trampoline would fail loudly at the transport, not silently
    corrupt the lookup tables.
    """

    def test_x25519_collision_caught(self) -> None:
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
        with pytest.raises(MemoryPolicyError) as ei:
            policy.check_write(0x4200, 0x117)  # historical stub injection
        assert "X25519" in str(ei.value)
        assert ei.value.region is not None
        assert ei.value.region.note == "X25519 RODATA + BSS"
