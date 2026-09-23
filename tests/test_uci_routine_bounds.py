"""``_execute_uci_routine`` refuses a routine that runs into its own reply buffer.

Every ``uci_*`` helper reads the reply the routine collected at ``$C200``
(``_RESP_ADDR``).  A routine at ``$C000`` that reaches ``$C200`` would have
the tail of its code overwritten by the response it is reading.  The largest
turbo-safe routine ends at ``$C1FD`` since the abort-acknowledgement wait
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
    with pytest.raises(ValueError, match="reply buffer"):
        un._execute_uci_routine(t, code, check_identifier=False)
    assert t.writes == []


def test_routine_ending_below_the_reply_buffer_is_uploaded() -> None:
    t = _Recorder()
    code = bytes(un._RESP_ADDR - un._CODE_ADDR)
    un._execute_uci_routine(t, code, check_identifier=False, timeout=1.0)
    assert (un._CODE_ADDR, len(code)) in t.writes


@pytest.mark.parametrize("name", [
    "build_uci_command", "build_get_ip", "build_tcp_connect",
    "build_udp_connect", "build_socket_write", "build_socket_read",
    "build_socket_close",
])
def test_every_turbo_routine_fits_below_the_reply_buffer(name: str) -> None:
    code = getattr(un, name)(turbo_safe=True)
    assert un._CODE_ADDR + len(code) <= un._RESP_ADDR
