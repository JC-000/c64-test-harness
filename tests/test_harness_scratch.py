"""Tests for ``HARNESS_SCRATCH`` — the machine-readable list of every fixed
address the harness itself writes during normal operation (issue #169).

The table in ``docs/memory_safety.md`` used to exist only as markdown, so
nothing could enforce it.  These tests pin the list to the code that
actually performs each write: if a routine grows past its declared span,
or a documented address is dropped from the list, the suite fails.
"""

from __future__ import annotations

import pytest

from c64_test_harness import (
    HARNESS_SCRATCH,
    MemoryRegion,
    ScratchRegion,
    harness_scratch_regions,
)


def _covering(addr: int) -> list[ScratchRegion]:
    return [r for r in HARNESS_SCRATCH if r.start <= addr < r.end]


# ---------------------------------------------------------------------------
# The issue's own measurement — every address in its table must be declared
# ---------------------------------------------------------------------------


class TestIssue169Table:
    @pytest.mark.parametrize(
        "addr",
        [
            0x0277,  # UCI keyboard dispatch
            0x0334,  # execute.jsr() trampoline
            0x0339,  # sid_player park JMP
            0x0360,  # execute.run_subroutine() trampoline
            0x03F0,  # run_subroutine() U64 flag bytes
            0xC000,  # UCI stub block
            0xC400,  # build_socket_write inner-loop scratch
            0xC500,  # uci_socket_write data buffer
            0xCF00,  # tests/test_vice_core.py::_restore_basic stub
            0x0000,  # snapshot.py CPU port direction
            0x0001,  # snapshot.py CPU port data
        ],
    )
    def test_every_issue_address_is_declared(self, addr: int) -> None:
        assert _covering(addr), f"${addr:04X} is not in HARNESS_SCRATCH"

    def test_cf00_restore_basic_stub_is_four_bytes(self) -> None:
        # CLI; JMP $E5CD — the stub test_vice_core.py::_restore_basic
        # writes on every screen/keyboard test.  Absent from every table
        # before #169.
        (r,) = _covering(0xCF00)
        assert r.start == 0xCF00
        assert r.length == 4
        assert "_restore_basic" in r.owner


# ---------------------------------------------------------------------------
# Shape of the list
# ---------------------------------------------------------------------------


class TestListShape:
    def test_sorted_by_start_then_end(self) -> None:
        keys = [(r.start, r.end) for r in HARNESS_SCRATCH]
        assert keys == sorted(keys)

    def test_every_entry_has_owner_and_purpose(self) -> None:
        for r in HARNESS_SCRATCH:
            assert r.owner.strip(), r
            assert r.purpose.strip(), r
            assert r.configurable.strip(), r

    def test_span_uses_inclusive_form(self) -> None:
        assert ScratchRegion(0x0334, 0x0339, "x", "y").span == "$0334-$0338"
        assert ScratchRegion(0x00C6, 0x00C7, "x", "y").span == "$00C6"

    def test_region_property_is_a_memory_region(self) -> None:
        r = ScratchRegion(0x0334, 0x0339, "execute.jsr", "trampoline")
        mr = r.region
        assert isinstance(mr, MemoryRegion)
        assert (mr.start, mr.end) == (0x0334, 0x0339)
        assert "execute.jsr" in mr.note

    def test_invalid_bounds_rejected(self) -> None:
        with pytest.raises(ValueError):
            ScratchRegion(0x0400, 0x0200, "x", "y")

    def test_harness_scratch_regions_excludes_transient_by_default(self) -> None:
        # The REU extract staging window ($0800-$87FF) is saved and
        # restored around the operation; a default that reserved 32 KiB
        # of the address space would be useless.
        default = harness_scratch_regions()
        everything = harness_scratch_regions(include_transient=True)
        assert all(isinstance(r, MemoryRegion) for r in default)
        assert len(everything) == len(HARNESS_SCRATCH)
        assert len(default) == len([r for r in HARNESS_SCRATCH if not r.transient])
        assert len(default) < len(everything)
        assert not any(r.start <= 0x0800 < r.end for r in default)


# ---------------------------------------------------------------------------
# Bounds pinned to the code that performs each write
# ---------------------------------------------------------------------------


def _region_of(owner_fragment: str, start: int) -> ScratchRegion:
    hits = [
        r for r in HARNESS_SCRATCH
        if owner_fragment in r.owner and r.start == start
    ]
    assert len(hits) == 1, (owner_fragment, hex(start), hits)
    return hits[0]


class TestBoundsMatchCode:
    def test_jsr_trampoline_is_five_bytes(self) -> None:
        # JSR abs (3) + NOP + NOP; checkpoint at scratch+3.
        r = _region_of("execute.jsr", 0x0334)
        assert r.length == 5
        assert "scratch_addr" in r.configurable

    def test_u64_run_subroutine_trampoline_is_fourteen_bytes(self) -> None:
        from c64_test_harness.execute import _build_u64_trampoline

        r = _region_of("run_subroutine", 0x0360)
        assert r.length == len(_build_u64_trampoline(0xC000, 0x03F0, 0x03F1))
        assert "trampoline_addr" in r.configurable

    def test_run_subroutine_flags_are_two_bytes(self) -> None:
        r = _region_of("run_subroutine", 0x03F0)
        assert r.length == 2

    def test_sid_player_stub_matches_build_vice_stub(self) -> None:
        from c64_test_harness.sid_player import (
            DEFAULT_STUB_ADDR,
            build_vice_stub,
        )

        r = _region_of("sid_player", DEFAULT_STUB_ADDR)
        assert r.length == len(build_vice_stub(0x1003, DEFAULT_STUB_ADDR))

    def test_sid_player_park_and_song_trampoline(self) -> None:
        from c64_test_harness.sid_player import _SONG_TRAMPOLINE_ADDR

        park = _region_of("sid_player", 0x0339)
        assert park.length == 3  # JMP ($A002)
        song = _region_of("sid_player", _SONG_TRAMPOLINE_ADDR)
        assert song.length == 6  # LDA #n; JSR init; RTS
        # The park JMP and the song trampoline abut, not overlap.
        assert park.end == song.start

    def test_sid_player_irq_vector(self) -> None:
        r = _region_of("sid_player", 0x0314)
        assert r.length == 2

    def test_uci_stub_block_matches_module_constants(self) -> None:
        from c64_test_harness import uci_network as u

        r = _region_of("uci_network", u._CODE_ADDR)
        for a in (
            u._DATA_ADDR, u._RESP_ADDR, u._STATUS_ADDR, u._RESP_LEN_ADDR,
            u._STAT_LEN_ADDR, u._SENTINEL_ADDR, u._ERROR_ADDR,
            u._SOCKET_ID_ADDR, u._DATA_BUF_ADDR, u._DATA_LEN_ADDR,
        ):
            assert r.start <= a < r.end, hex(a)
        assert r.end == u._ERROR_ADDR + 1

    def test_uci_inner_loop_and_socket_id_slots(self) -> None:
        from c64_test_harness import uci_network as u

        inner = _region_of("build_socket_write", u._INNER_LOOP_CNT_LO)
        assert inner.end == u._INNER_LOOP_Y_SAVE + 1
        sid = _region_of("uci_socket_write", u._WRITE_SOCKET_ID_ADDR)
        assert sid.length == 1

    def test_uci_write_buffer_covers_max_payload_plus_length(self) -> None:
        from c64_test_harness import uci_network as u

        r = _region_of("uci_socket_write", u._WRITE_DATA_BUF_ADDR)
        # data_len_addr = data_area + len(data), 2 bytes LE — so the
        # last byte touched is data_area + SOCKET_WRITE_MAX_BYTES + 1.
        assert r.end == u._WRITE_DATA_BUF_ADDR + u.SOCKET_WRITE_MAX_BYTES + 2

    def test_keyboard_buffer_is_kernal_ten_bytes(self) -> None:
        # $0277-$0280 KEYD (10 bytes) + $00C6 NDX.
        kb = _region_of("inject_keys", 0x0277)
        assert kb.length == 10
        ndx = _region_of("inject_keys", 0x00C6)
        assert ndx.length == 1

    def test_liveness_probe_span(self) -> None:
        from c64_test_harness.backends import ultimate64_probe as p

        r = _region_of("liveness_probe", p._LIVENESS_PROBE_ADDR)
        assert r.length == p._LIVENESS_PROBE_LEN
        assert r.transient  # original bytes are read first and restored

    def test_bridge_ping_defaults_cover_largest_routine(self) -> None:
        from c64_test_harness import bridge_ping as bp

        peek = _region_of("bridge_ping", bp._DEFAULT_PEEK_ADDR)
        # Exact, not >=: the declared span must move when the routine does.
        assert peek.length == len(
            bp.build_rx_peek_code(load_addr=bp._DEFAULT_PEEK_ADDR, result_addr=0xC0FF)
        )
        consume = _region_of("bridge_ping", bp._DEFAULT_CONSUME_ADDR)
        largest = max(
            len(bp.build_tx_code(
                load_addr=bp._DEFAULT_CONSUME_ADDR, frame_buf=0xC400,
                frame_len=1514, result_addr=0xC0FF,
            )),
            len(bp.build_read_and_match_echo_reply_code(
                load_addr=bp._DEFAULT_CONSUME_ADDR, rx_buf=0x8000,
                result_addr=0xC0FF, identifier=1, sequence=1,
            )),
            len(bp.build_read_and_respond_echo_request_code(
                load_addr=bp._DEFAULT_CONSUME_ADDR, rx_buf=0x8000,
                my_ip=bytes(4), result_addr=0xC0FF,
            )),
        )
        assert consume.length == largest  # tx 79, match 143, respond 357

    def test_reu_staging_window_matches_snapshot_constants(self) -> None:
        from c64_test_harness import snapshot as s

        r = _region_of("extract_reu_contents", s._REU_STAGING_BASE)
        assert r.length == s._REU_STAGING_SIZE
        assert r.transient

    def test_cpu_port_is_two_single_byte_writes(self) -> None:
        r = _region_of("restore_snapshot", 0x0000)
        assert r.length == 2


# ---------------------------------------------------------------------------
# Transient flag — only the two save-and-write-back spans may carry it
# ---------------------------------------------------------------------------

_TRANSIENT_OWNERS = (
    "backends.ultimate64_probe.liveness_probe",
    "snapshot.extract_reu_contents",
)
_NON_TRANSIENT_STARTS = [
    0x0000, 0x00C6, 0x0277, 0x0314, 0x0334, 0x0339, 0x033C, 0x0360, 0x03F0,
    0xC000, 0xC100, 0xC400, 0xC403, 0xC500, 0xCF00,
]


class TestTransientFlag:
    """Flipping any scratch entry to ``transient=True`` silently makes it
    allocatable again (the arbiter skips transient entries).  Only the
    drift test would notice — so pin the flag on every entry here."""

    @pytest.mark.parametrize("start", _NON_TRANSIENT_STARTS)
    def test_non_transient_entries_stay_non_transient(self, start: int) -> None:
        entries = [
            r for r in HARNESS_SCRATCH
            if r.start == start and r.owner not in _TRANSIENT_OWNERS
        ]
        assert entries, f"no non-transient entry starts at ${start:04X}"
        for r in entries:
            assert r.transient is False, r

    def test_only_the_two_known_owners_are_transient(self) -> None:
        transient_owners = sorted({r.owner for r in HARNESS_SCRATCH if r.transient})
        assert transient_owners == sorted(_TRANSIENT_OWNERS)

    def test_transient_entries_pinned_by_span(self) -> None:
        # Owner-set pinning alone misses a second transient entry under a
        # known owner at a new start.  Pin the exact (start, end-exclusive)
        # spans: liveness probe $0334-$03B3 and REU staging $0800-$87FF.
        spans = sorted((r.start, r.end) for r in HARNESS_SCRATCH if r.transient)
        assert spans == [(0x0334, 0x03B4), (0x0800, 0x8800)]

    def test_every_start_in_the_list_is_covered_by_this_test(self) -> None:
        starts = sorted({r.start for r in HARNESS_SCRATCH if not r.transient})
        assert starts == sorted(_NON_TRANSIENT_STARTS)
