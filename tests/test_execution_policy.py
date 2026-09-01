"""#166: refuse a jsr() into a span the caller declared dead.

Calling into reclaimed code does not fault cleanly. The entry point
survives the reclaim, so the CPU executes real instructions before
running off into zeroed RAM ($00 = BRK) and wedging somewhere
unpredictable. The only symptom is a TimeoutError naming nothing, which
is indistinguishable from a hung emulator, host contention or a
genuinely slow routine -- three suites hit it in one day and all three
first blamed the emulator.

The defensive guard people actually write does not help::

    if "reu_mul_init" in labels:        # True. Always True.
        jsr(transport, labels["reu_mul_init"])

The reclaim removes the *code*, not the *symbol*: the label survives as
a real link-time address. So the line reads as "only call this if this
build has it" and is satisfied by every build.

Spans are derived from symbols, never pinned. Two builds a few merges
apart put the same entry at $87BB and $87D1, so a guard holding a
literal address would be wrong by the next relink and would then be a
comment wearing a test's costume.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from c64_test_harness.execute import jsr
from c64_test_harness.execution_policy import (
    ExecutionPolicy,
    ExecutionPolicyError,
)
from c64_test_harness.labels import Labels
from c64_test_harness.memory_policy import MemoryRegion


def _labels(**syms: int) -> Labels:
    labels = Labels()
    for name, addr in syms.items():
        labels._by_name[name] = addr
        labels._by_addr[addr] = name
    return labels


#: The consumer shape from the issue: an init segment reclaimed as BSS,
#: with a routine sitting inside it. The segment start and the routine
#: are deliberately *different* addresses -- the issue notes the entry
#: point "sits below the boundary where the zero-fill starts", and
#: co-locating them would make the symbol lookup ambiguous (Labels is
#: last-write-wins on address, see test_colocated_symbols_are_ambiguous).
def _reclaim_labels(entry: int = 0x87BB, end: int = 0x8800) -> Labels:
    seg_start = 0x8780
    return _labels(
        reu_mul_init=entry,
        __LIB_X25519_INIT_CODE_LOAD__=seg_start,
        __LIB_X25519_INIT_CODE_SIZE__=end - seg_start,
    )


def _policy(labels: Labels) -> ExecutionPolicy:
    return ExecutionPolicy.from_labels(
        labels,
        dead=[("__LIB_X25519_INIT_CODE_LOAD__", "__LIB_X25519_INIT_CODE_SIZE__")],
        reason="reclaimed as BSS after boot; see c64-wireguard#107",
    )


class TestSpanDerivation:
    def test_span_comes_from_the_symbols_not_a_literal(self) -> None:
        """The same declaration must follow the segment across a relink.

        $87BB and $87D1 are two real builds a few merges apart; the
        declaration names symbols, so both are covered by the same line.
        """
        for entry in (0x87BB, 0x87D1):
            policy = _policy(_reclaim_labels(entry))
            assert policy.dead_regions[0].contains_addr(entry)
            assert policy.dead_regions[0].end == 0x8800

    def test_a_relink_that_moves_the_segment_moves_the_span(self) -> None:
        labels = _labels(
            __SEG_LOAD__=0x9000, __SEG_SIZE__=0x100, routine=0x9040
        )
        policy = ExecutionPolicy.from_labels(
            labels, dead=[("__SEG_LOAD__", "__SEG_SIZE__")], reason="r"
        )
        assert policy.dead_regions[0] == MemoryRegion(
            0x9000, 0x9100, "__SEG_LOAD__: r"
        )

    def test_size_symbol_is_a_length_not_an_end_address(self) -> None:
        policy = _policy(_reclaim_labels(0x87BB, 0x8800))
        region = policy.dead_regions[0]
        assert region.start == 0x8780
        assert region.length == 0x8800 - 0x8780
        assert region.end == 0x8800

    def test_missing_symbol_is_rejected_at_declaration(self) -> None:
        """Fail where it can be fixed, not on the next call."""
        with pytest.raises(ValueError, match="__NOPE__"):
            ExecutionPolicy.from_labels(
                _reclaim_labels(),
                dead=[("__NOPE__", "__LIB_X25519_INIT_CODE_SIZE__")],
                reason="x",
            )

    @pytest.mark.parametrize("size", [0, -4])
    def test_empty_span_is_rejected_naming_the_size_symbol(
        self, size: int
    ) -> None:
        """MemoryRegion would reject it anyway -- the message is the point.

        Its own error says "MemoryRegion $8000-$8000 has end <= start",
        which does not tell you *which declaration* produced it. An
        earlier version of this test only asserted ValueError, so it
        passed with the guard removed and pinned nothing.
        """
        labels = _labels(__SEG_LOAD__=0x8000, __SEG_SIZE__=size)
        with pytest.raises(ValueError) as excinfo:
            ExecutionPolicy.from_labels(
                labels, dead=[("__SEG_LOAD__", "__SEG_SIZE__")], reason="x"
            )
        msg = str(excinfo.value)
        assert "__SEG_SIZE__" in msg, msg
        assert "__SEG_LOAD__" in msg, msg

    def test_permissive_policy_declares_nothing(self) -> None:
        assert ExecutionPolicy.permissive().dead_regions == ()


class TestCheckCall:
    def test_entry_address_is_refused(self) -> None:
        policy = _policy(_reclaim_labels())
        with pytest.raises(ExecutionPolicyError) as excinfo:
            policy.check_call(0x87BB)
        assert excinfo.value.addr == 0x87BB

    def test_an_address_inside_the_span_is_refused(self) -> None:
        policy = _policy(_reclaim_labels())
        with pytest.raises(ExecutionPolicyError):
            policy.check_call(0x87F0)

    def test_the_exclusive_end_is_allowed(self) -> None:
        """Half-open, matching MemoryRegion."""
        _policy(_reclaim_labels()).check_call(0x8800)

    def test_an_address_outside_is_allowed(self) -> None:
        _policy(_reclaim_labels()).check_call(0x0810)

    def test_message_names_address_symbol_span_and_reason(self) -> None:
        """The message is the deliverable -- it is what a timeout lacked."""
        policy = _policy(_reclaim_labels())
        with pytest.raises(ExecutionPolicyError) as excinfo:
            policy.check_call(0x87BB)
        msg = str(excinfo.value)
        assert "$87BB" in msg
        assert "reu_mul_init" in msg
        assert "$8780-$87FF" in msg  # the declared segment, not the entry
        assert "reclaimed as BSS" in msg
        assert "c64-wireguard#107" in msg

    def test_message_copes_with_an_unnamed_address(self) -> None:
        policy = _policy(_reclaim_labels())
        with pytest.raises(ExecutionPolicyError) as excinfo:
            policy.check_call(0x87F0)
        assert "$87F0" in str(excinfo.value)

    def test_colocated_symbols_are_ambiguous(self) -> None:
        """A known limitation, pinned rather than papered over.

        ``Labels`` keeps one name per address, last write wins, so when a
        routine and a segment symbol share an address the message names
        whichever the label file listed last. The address and the span
        are still exact; only the friendly name is a coin toss.
        """
        labels = _labels(__SEG_LOAD__=0x9000, __SEG_SIZE__=0x100)
        labels._by_name["entry"] = 0x9000
        labels._by_addr[0x9000] = "entry"
        policy = ExecutionPolicy.from_labels(
            labels, dead=[("__SEG_LOAD__", "__SEG_SIZE__")], reason="r"
        )
        with pytest.raises(ExecutionPolicyError) as excinfo:
            policy.check_call(0x9000)
        assert excinfo.value.symbol == "entry"
        assert "$9000" in str(excinfo.value)


class TestOverride:
    def test_reason_string_permits_the_call(self, caplog) -> None:
        policy = _policy(_reclaim_labels())
        import c64_test_harness.execution_policy as ep

        with caplog.at_level(logging.WARNING, logger=ep.__name__):
            policy.check_call(0x87BB, override="verifying it really wedges")
        assert any(
            r.levelno == logging.WARNING
            and "verifying it really wedges" in r.getMessage()
            for r in caplog.records
        ), caplog.text

    def test_bare_true_is_rejected(self) -> None:
        policy = _policy(_reclaim_labels())
        with pytest.raises(ValueError, match="non-empty reason string"):
            policy.check_call(0x87BB, override=True)

    def test_empty_reason_is_rejected(self) -> None:
        policy = _policy(_reclaim_labels())
        with pytest.raises(ValueError, match="non-empty reason string"):
            policy.check_call(0x87BB, override="")


@pytest.fixture
def stub_monitor(monkeypatch):
    """Stub jsr()'s breakpoint machinery.

    These tests are about whether the guard lets the call through, not
    about the monitor round-trip; without this a MagicMock transport
    falls into real checkpoint code and dies formatting a mock.
    """
    import c64_test_harness.execute as ex

    monkeypatch.setattr(ex, "set_breakpoint", lambda *a, **k: 1)
    monkeypatch.setattr(ex, "delete_breakpoint", lambda *a, **k: None)
    monkeypatch.setattr(ex, "wait_for_pc", lambda *a, **k: {"PC": 0x0337})
    return ex


class TestJsrIntegration:
    def test_jsr_refuses_before_touching_the_machine(self) -> None:
        """Prevention, not recovery: nothing reaches the transport.

        This is what separates #166 from #156 -- there is no wedged
        machine to recover, because the call never happened.
        """
        transport = MagicMock()
        transport.execution_policy = _policy(_reclaim_labels())
        with pytest.raises(ExecutionPolicyError):
            jsr(transport, 0x87BB)
        transport.write_memory.assert_not_called()
        transport.set_registers.assert_not_called()
        transport.resume.assert_not_called()

    def test_jsr_allows_a_live_address(self, stub_monitor) -> None:
        transport = MagicMock()
        transport.execution_policy = _policy(_reclaim_labels())
        jsr(transport, 0x0810)
        transport.write_memory.assert_called_once()

    def test_jsr_without_a_policy_is_unchanged(self, stub_monitor) -> None:
        """Permissive by default: no declaration, no behaviour change."""
        transport = MagicMock(spec=["write_memory", "set_registers", "resume"])
        jsr(transport, 0x87BB)
        transport.write_memory.assert_called_once()

    def test_jsr_override_reaches_the_policy(self, stub_monitor) -> None:
        transport = MagicMock()
        transport.execution_policy = _policy(_reclaim_labels())
        jsr(transport, 0x87BB, override="deliberately calling the dead span")
        transport.write_memory.assert_called_once()

    def test_jsr_trampoline_address_is_not_checked(self, stub_monitor) -> None:
        """The guard is about the callee, not the harness's own scratch.

        jsr() writes its trampoline at $0334 and sets PC there; that is
        harness-owned and never the caller's declared span.
        """
        transport = MagicMock()
        transport.execution_policy = ExecutionPolicy.from_regions(
            [MemoryRegion(0x0334, 0x0340, "harness scratch")],
            reason="not a real declaration",
        )
        jsr(transport, 0x0810)
        transport.write_memory.assert_called_once()
