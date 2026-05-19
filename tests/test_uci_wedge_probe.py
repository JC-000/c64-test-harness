"""Unit tests for the UCI wedge-detection primitives (issue #112).

All transport interactions are mocked — no emulator or hardware is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.uci_network import (
    UCI_CONTROL_STATUS_REG,
    UCI_FENCE_OUTER,
    UCI_FENCE_INNER,
    build_uci_status_peek,
    uci_status_peek,
    uci_wedge_probe,
    UciWedgeProbeResult,
)


# ---------------------------------------------------------------------------
# Memory layout constants — kept private to this test module so tests stay
# legible if the underlying module reshuffles its addresses.
# ---------------------------------------------------------------------------
_SENTINEL_ADDR = 0xC3FE
_ERROR_ADDR    = 0xC3FF
_RESP_ADDR     = 0xC200
_SENTINEL_DONE = 0x42

# 6502 opcodes we assert against.
_LDA_ABS = 0xAD
_STA_ABS = 0x8D
_RTS     = 0x60
_BNE     = 0xD0
_BEQ     = 0xF0
_JMP_ABS = 0x4C


# ---------------------------------------------------------------------------
# Builder tests
# ---------------------------------------------------------------------------

class TestBuildUciStatusPeek:
    """Tests for build_uci_status_peek() assembly builder."""

    def test_build_uci_status_peek_emits_lda_dF1C_no_wait_loop(self) -> None:
        """First instruction must be LDA $DF1C; no spin-loop on status."""
        code = build_uci_status_peek()

        # Must start with LDA $DF1C (the non-blocking peek).
        assert code[0] == _LDA_ABS
        assert code[1] == (UCI_CONTROL_STATUS_REG & 0xFF)
        assert code[2] == ((UCI_CONTROL_STATUS_REG >> 8) & 0xFF)

        # Must end with RTS (routine is dispatched via SYS).
        assert code[-1] == _RTS

        # NO branch-to-self that would form a spin loop. We allow forward
        # branches (used by fence helpers) but not back-edges. With the
        # plain (non-turbo) build there is no branch at all.
        for i in range(len(code) - 1):
            op = code[i]
            if op in (_BNE, _BEQ):
                offset = code[i + 1]
                if offset >= 0x80:
                    # Signed back-branch — disallow: that's a spin shape.
                    pytest.fail(
                        f"unexpected back-branch at offset {i}: "
                        f"opcode 0x{op:02X} offset 0x{offset:02X}"
                    )

        # No JMP at all in the plain build — any JMP would form a spin.
        for i in range(len(code) - 2):
            if code[i] == _JMP_ABS:
                target = code[i + 1] | (code[i + 2] << 8)
                pytest.fail(
                    f"unexpected JMP at offset {i} (target ${target:04X}) "
                    f"in non-turbo build — would form a spin loop"
                )

    def test_build_uci_status_peek_turbo_safe_includes_fence(self) -> None:
        """turbo_safe=True path must insert the delay-loop fence."""
        plain = build_uci_status_peek()
        fenced = build_uci_status_peek(turbo_safe=True)

        # Fence adds bytes between LDA and STA — fenced must be larger.
        assert len(fenced) > len(plain)

        # Look for the fence signature: PHA TXA PHA LDX #OUTER LDY #INNER.
        fence_sig = bytes([0x48, 0x8A, 0x48, 0xA2, UCI_FENCE_OUTER,
                           0xA0, UCI_FENCE_INNER])
        assert fence_sig in fenced, (
            "turbo_safe=True must emit the standard fence signature"
        )
        assert fence_sig not in plain, (
            "plain (turbo_safe=False) build must NOT include the fence"
        )

    def test_build_uci_status_peek_returns_bytes(self) -> None:
        code = build_uci_status_peek()
        assert isinstance(code, bytes)
        assert len(code) >= 6  # LDA(3) + STA(3) at minimum, plus sentinel+RTS


# ---------------------------------------------------------------------------
# High-level wrapper tests (mocked transport)
# ---------------------------------------------------------------------------

def _make_mock_transport(peek_byte: int = 0x00) -> MagicMock:
    """Mock transport: completes one SYS dispatch and returns *peek_byte* at $C200.

    Mirrors tests/test_uci_network.py::_make_mock_transport but tailored
    for the single-byte status-peek shape.
    """
    t = MagicMock()
    call_count = {"sentinel_polls": 0}

    def mock_read_memory(addr: int, length: int) -> bytes:
        if addr == _SENTINEL_ADDR:
            call_count["sentinel_polls"] += 1
            # First poll returns 0 (not yet done), second returns sentinel.
            if call_count["sentinel_polls"] >= 2:
                call_count["sentinel_polls"] = 0  # reset for next peek
                return bytes([_SENTINEL_DONE])
            return b"\x00"
        if addr == _ERROR_ADDR:
            return b"\x00"
        if addr == _RESP_ADDR:
            return bytes([peek_byte]) * length
        return bytes(length)

    t.read_memory.side_effect = mock_read_memory
    return t


def _make_sequence_mock_transport(peek_bytes: list[int]) -> MagicMock:
    """Mock transport whose successive $C200 reads return *peek_bytes* in order."""
    t = MagicMock()
    state = {"sentinel_polls": 0, "resp_idx": 0}

    def mock_read_memory(addr: int, length: int) -> bytes:
        if addr == _SENTINEL_ADDR:
            state["sentinel_polls"] += 1
            if state["sentinel_polls"] >= 2:
                state["sentinel_polls"] = 0
                return bytes([_SENTINEL_DONE])
            return b"\x00"
        if addr == _ERROR_ADDR:
            return b"\x00"
        if addr == _RESP_ADDR:
            idx = state["resp_idx"]
            byte = peek_bytes[idx] if idx < len(peek_bytes) else 0x00
            state["resp_idx"] += 1
            return bytes([byte]) * length
        return bytes(length)

    t.read_memory.side_effect = mock_read_memory
    return t


class TestUciStatusPeek:
    """Tests for uci_status_peek() helper."""

    def test_uci_status_peek_returns_byte_from_resp_addr(self) -> None:
        t = _make_mock_transport(peek_byte=0x31)
        result = uci_status_peek(t, timeout=1.0)
        assert result == 0x31
        assert isinstance(result, int)

    def test_uci_status_peek_writes_code_to_memory(self) -> None:
        t = _make_mock_transport(peek_byte=0x00)
        uci_status_peek(t, timeout=1.0)
        assert t.write_memory.call_count >= 1


# ---------------------------------------------------------------------------
# uci_wedge_probe classification tests
# ---------------------------------------------------------------------------

class TestUciWedgeProbe:
    """Tests for uci_wedge_probe() classification rules."""

    def test_uci_wedge_probe_idle(self) -> None:
        """All-zero samples => idle, no recommendation."""
        t = _make_sequence_mock_transport([0x00, 0x00, 0x00, 0x00])
        result = uci_wedge_probe(t, samples=4, sample_interval=0.0, timeout=1.0)

        assert isinstance(result, UciWedgeProbeResult)
        assert result.classification == "idle"
        assert result.is_idle is True
        assert result.is_wedged is False
        assert result.recommendation is None
        assert result.samples == (0x00, 0x00, 0x00, 0x00)

    def test_uci_wedge_probe_wedged(self) -> None:
        """All samples have STATE|CMD_BUSY stuck => wedged, recommend power-cycle."""
        t = _make_sequence_mock_transport([0x31, 0x31, 0x31, 0x31])
        result = uci_wedge_probe(t, samples=4, sample_interval=0.0, timeout=1.0)

        assert result.classification == "wedged"
        assert result.is_wedged is True
        assert result.is_idle is False
        assert result.recommendation is not None
        assert "physical power-cycle" in result.recommendation
        assert result.samples == (0x31, 0x31, 0x31, 0x31)

    def test_uci_wedge_probe_busy_transient(self) -> None:
        """Mixed busy/idle samples => busy_transient."""
        t = _make_sequence_mock_transport([0x10, 0x00, 0x10, 0x00])
        result = uci_wedge_probe(t, samples=4, sample_interval=0.0, timeout=1.0)

        assert result.classification == "busy_transient"
        assert result.is_wedged is False
        assert result.is_idle is False
        assert result.recommendation is not None
        assert "cycling normally" in result.recommendation
        assert result.samples == (0x10, 0x00, 0x10, 0x00)

    def test_uci_wedge_probe_requires_minimum_samples(self) -> None:
        """samples<2 must raise ValueError (cannot distinguish wedged vs transient)."""
        t = _make_sequence_mock_transport([0x00])
        with pytest.raises(ValueError, match="samples >= 2"):
            uci_wedge_probe(t, samples=1, sample_interval=0.0, timeout=1.0)

    def test_uci_wedge_probe_two_samples_wedged(self) -> None:
        """Minimum-size (samples=2) wedged classification still works."""
        t = _make_sequence_mock_transport([0x31, 0x31])
        result = uci_wedge_probe(t, samples=2, sample_interval=0.0, timeout=1.0)
        assert result.classification == "wedged"
        assert result.is_wedged is True

    def test_uci_wedge_probe_result_is_frozen(self) -> None:
        """UciWedgeProbeResult should be immutable (frozen dataclass)."""
        result = UciWedgeProbeResult(
            samples=(0x00,), classification="idle", recommendation=None,
        )
        with pytest.raises((AttributeError, Exception)):
            result.classification = "wedged"  # type: ignore[misc]
