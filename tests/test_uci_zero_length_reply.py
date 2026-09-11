"""A zero-length UCI reply must surface the firmware's status string.

The firmware has three zero-length-reply branches for ``GET_IPADDR``,
each with a distinguishing ASCII status (``network_target.cc:80-100``):
``"81,INVALID PARAMS"``, ``"82,PARAMETER(S) OUT OF RANGE"``,
``"83,INTERFACE NOT AVAILABLE"``.  ``build_uci_command`` already emits
``_build_read_status``, so that string is sitting at ``$C300`` with its
length at ``$C3F2`` when the helper returns -- and ``uci_get_ip``
returned ``""`` without ever reading either (issue #273 class 1).

Why the error flag is not the channel for this: ``$C3FF``
(``error_busy``) is raised by the FPGA only when PUSH arrives while the
state machine is non-idle (``command_protocol.vhd:157``) -- a protocol
fault.  A firmware status of ``"82,..."`` is a perfectly well-formed
reply and leaves it clear, which is why ``_execute_uci_routine``'s
existing error branch never fires for these.  Every mock below therefore
answers ``$C3FF`` with zero, exactly as the device did.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.uci_network import (
    _ERROR_ADDR,
    _RESP_ADDR,
    _RESP_LEN_ADDR,
    _SENTINEL_ADDR,
    _SENTINEL_DONE,
    _STAT_LEN_ADDR,
    _STATUS_ADDR,
    UCIError,
    uci_get_interface_count,
    uci_get_ip,
)

#: One of the three firmware branches; the one triage rates most likely
#: for the observed failure (zero interfaces registered).
STATUS_82 = b"82,PARAMETER(S) OUT OF RANGE"


def _empty_reply_transport(status: bytes = STATUS_82) -> MagicMock:
    """A transport whose routine completed with a zero-length reply.

    Sentinel set, error flag clear, ``$C3F0`` zero, status string
    present -- the exact state the U64E left behind.
    """
    t = MagicMock()
    polls = {"n": 0}

    def read_memory(addr: int, length: int) -> bytes:
        if addr == _SENTINEL_ADDR:
            polls["n"] += 1
            return bytes([_SENTINEL_DONE]) if polls["n"] >= 2 else b"\x00"
        if addr == _ERROR_ADDR:
            return b"\x00"           # no protocol fault -- see module docstring
        if addr == _RESP_LEN_ADDR:
            return b"\x00"           # zero-length reply
        if addr == _STAT_LEN_ADDR:
            return bytes([len(status)])
        if _STATUS_ADDR <= addr < _STATUS_ADDR + max(len(status), 1):
            off = addr - _STATUS_ADDR
            return status[off:off + length]
        if addr == _RESP_ADDR:
            return bytes(length)
        return bytes(length)

    t.read_memory.side_effect = read_memory
    return t


class TestGetIpOnEmptyReply:
    def test_raises_instead_of_returning_empty_string(self) -> None:
        t = _empty_reply_transport()
        with pytest.raises(UCIError):
            uci_get_ip(t, timeout=1.0)

    def test_error_carries_the_firmware_status_string(self) -> None:
        t = _empty_reply_transport()
        with pytest.raises(UCIError) as exc:
            uci_get_ip(t, timeout=1.0)
        assert "82,PARAMETER(S) OUT OF RANGE" in str(exc.value)

    @pytest.mark.parametrize(
        "status",
        [b"81,INVALID PARAMS", b"82,PARAMETER(S) OUT OF RANGE",
         b"83,INTERFACE NOT AVAILABLE"],
        ids=["81", "82", "83"],
    )
    def test_each_firmware_branch_is_distinguishable(
        self, status: bytes
    ) -> None:
        """The point of reading it is telling the three branches apart."""
        t = _empty_reply_transport(status)
        with pytest.raises(UCIError) as exc:
            uci_get_ip(t, timeout=1.0)
        assert status.decode() in str(exc.value)

    def test_reads_the_status_length_and_the_status_bytes(self) -> None:
        t = _empty_reply_transport()
        with pytest.raises(UCIError):
            uci_get_ip(t, timeout=1.0)
        addrs = [c.args[0] for c in t.read_memory.call_args_list]
        assert _STAT_LEN_ADDR in addrs, "never read the status length"
        assert _STATUS_ADDR in addrs, "never read the status string"

    def test_an_absent_status_still_raises(self) -> None:
        """A zero-length reply is a failure with or without a string.

        Silently returning ``""`` is the defect; a firmware that gives no
        status must not buy back the silence.
        """
        t = _empty_reply_transport(b"")
        with pytest.raises(UCIError):
            uci_get_ip(t, timeout=1.0)


class TestGetInterfaceCountOnEmptyReply:
    def test_raises_instead_of_returning_zero(self) -> None:
        """``0`` is a plausible answer, which is what makes it dangerous."""
        t = _empty_reply_transport()
        with pytest.raises(UCIError):
            uci_get_interface_count(t, timeout=1.0)

    def test_error_carries_the_firmware_status_string(self) -> None:
        t = _empty_reply_transport()
        with pytest.raises(UCIError) as exc:
            uci_get_interface_count(t, timeout=1.0)
        assert "82,PARAMETER(S) OUT OF RANGE" in str(exc.value)


def _error_flag_transport(status: bytes) -> MagicMock:
    """A transport whose routine set ``$C3FF`` -- the protocol-fault path.

    Distinct from the zero-length case above: here the reply is
    malformed, not merely empty.  Covered because the status read on
    this path was refactored onto the same helper.
    """
    t = _empty_reply_transport(status)
    inner = t.read_memory.side_effect

    def read_memory(addr: int, length: int) -> bytes:
        if addr == _ERROR_ADDR:
            return b"\x01"
        return inner(addr, length)

    t.read_memory.side_effect = read_memory
    return t


class TestErrorFlagPathUnchanged:
    def test_status_string_still_reaches_the_exception(self) -> None:
        with pytest.raises(UCIError) as exc:
            uci_get_ip(_error_flag_transport(b"81,INVALID PARAMS"), timeout=1.0)
        assert "UCI command failed" in str(exc.value)
        assert "81,INVALID PARAMS" in str(exc.value)

    def test_absent_status_keeps_the_generic_message(self) -> None:
        with pytest.raises(UCIError) as exc:
            uci_get_ip(_error_flag_transport(b""), timeout=1.0)
        assert str(exc.value) == "UCI command returned error"
