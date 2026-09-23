"""``_execute_uci_routine`` refuses a routine that runs into its own reply buffer.

Every ``uci_*`` helper reads the reply the routine collected at ``$C200``
(``_RESP_ADDR``).  A routine at ``$C000`` that reaches ``$C200`` would have
the tail of its code overwritten by the response it is reading.  The largest
turbo-safe routine's last byte is ``$C1FC`` since the abort-acknowledgement wait
(#419) grew every turbo preamble by 18 bytes, so the margin is three bytes.
"""
from __future__ import annotations

import pytest

from c64_test_harness import uci_network as un


class _Recorder:
    """Records writes; answers reads with the sentinel so the call completes."""

    def __init__(self) -> None:
        self.writes: list[tuple[int, int]] = []

    def write_memory(self, addr: int, data) -> None:
        self.writes.append((addr, len(bytes(data))))

    def read_memory(self, addr: int, length: int) -> bytes:
        if addr == un._SENTINEL_ADDR:
            return bytes([un._SENTINEL_DONE])
        return bytes(length)


def test_routine_reaching_the_reply_buffer_is_refused_before_any_write() -> None:
    t = _Recorder()
    code = bytes(un._RESP_ADDR - un._CODE_ADDR + 1)
    with pytest.raises(ValueError, match="reply area"):
        un._execute_uci_routine(t, code, check_identifier=False)
    assert t.writes == []


def test_routine_ending_below_the_reply_buffer_is_uploaded() -> None:
    t = _Recorder()
    code = bytes(un._RESP_ADDR - un._CODE_ADDR)
    un._execute_uci_routine(t, code, check_identifier=False, timeout=1.0)
    assert (un._CODE_ADDR, len(code)) in t.writes


#: The routine's working area: reply at $C200, status at $C300, the length
#: words at $C3F0/$C3F2, sentinel $C3FE and error flag $C3FF.
_AREA_END = un._ERROR_ADDR + 1  # one past $C3FF


@pytest.mark.parametrize("addr,length", [
    (0xC250, 16),                    # wholly inside the reply buffer
    (0xC3F8, 8),                     # over the length words, sentinel, error
    (un._ERROR_ADDR, 1),             # the error flag alone
], ids=["reply", "sentinel-and-error", "error-flag"])
def test_routine_placed_inside_the_reply_area_is_refused(addr, length) -> None:
    """Not only $C200 itself: code anywhere in $C200-$C3FF is overwritten by
    the reply, the status, or the sentinel/error writes it waits on."""
    t = _Recorder()
    with pytest.raises(ValueError, match="reply area"):
        un._execute_uci_routine(
            t, bytes(length), code_addr=addr, check_identifier=False
        )
    assert t.writes == []


def test_routine_placed_past_the_reply_area_is_uploaded() -> None:
    """The guard is an overlap test, not "anything ending past $C200"."""
    t = _Recorder()
    code = bytes(64)
    un._execute_uci_routine(
        t, code, code_addr=_AREA_END, check_identifier=False, timeout=1.0
    )
    assert (_AREA_END, len(code)) in t.writes


# --- callers whose routine writes elsewhere pass their own output spans ----

def test_a_routine_over_an_unused_reply_buffer_is_uploaded_with_its_spans() -> None:
    """#420's multi-block read is 620 B at $C000 (to $C26B) and never writes
    $C200-$C2FF: its output is the status page and its own buffer."""
    t = _Recorder()
    code = bytes(620)
    un._execute_uci_routine(
        t, code, check_identifier=False, timeout=1.0,
        output_spans=((0xC300, 0xC3FF), (0xC500, 0xC8FF)),
    )
    assert (un._CODE_ADDR, len(code)) in t.writes


def test_a_routine_over_a_declared_output_span_is_refused() -> None:
    t = _Recorder()
    with pytest.raises(ValueError, match=r"\$C500-\$C8FF"):
        un._execute_uci_routine(
            t, bytes(32), code_addr=0xC4F0, check_identifier=False,
            output_spans=((0xC300, 0xC3FF), (0xC500, 0xC8FF)),
        )
    assert t.writes == []


def test_the_sentinel_and_error_flag_are_protected_whatever_the_spans() -> None:
    """The host clears and polls them, so no span list can give them away."""
    t = _Recorder()
    with pytest.raises(ValueError, match="sentinel|error"):
        un._execute_uci_routine(
            t, bytes(4), code_addr=un._SENTINEL_ADDR - 1,
            check_identifier=False, output_spans=((0xC500, 0xC5FF),),
        )
    assert t.writes == []


def test_a_reversed_output_span_is_refused() -> None:
    """A reversed pair would overlap nothing and quietly disarm the guard."""
    t = _Recorder()
    with pytest.raises(ValueError, match="reversed"):
        un._execute_uci_routine(
            t, bytes(4), check_identifier=False, output_spans=((0xC3FF, 0xC300),),
        )
    assert t.writes == []


@pytest.mark.parametrize("name", [
    "build_uci_command", "build_get_ip", "build_tcp_connect",
    "build_udp_connect", "build_socket_write", "build_socket_read",
    "build_socket_close",
])
def test_every_turbo_routine_fits_below_the_reply_buffer(name: str) -> None:
    code = getattr(un, name)(turbo_safe=True)
    assert un._CODE_ADDR + len(code) <= un._RESP_ADDR
