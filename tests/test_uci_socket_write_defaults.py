"""``build_socket_write``'s default inputs must not sit inside its routine (#346).

Same shape as #322.  With ``turbo_safe=True`` the routine is 446 B at
``$C000-$C1BD``, and the old defaults put the socket id at ``$C100``, the data
at ``$C101`` and the length at ``$C1FF`` -- all inside the routine.  A direct
caller that stages its input at the defaults and then uploads the routine
gets its input overwritten by code.

``uci_socket_write`` already passes ``$C403``/``$C500``/``$C500+len``
explicitly, so the helper was never affected; this is about the exported
builder.  The fix resolves the defaults per ``turbo_safe`` (plain keeps
``$C100``/``$C101``/``$C1FF`` byte-identically; turbo uses ``$C403`` for the
socket id, ``$C500`` for the data and ``$C87C`` -- ``$C500 + 892``, the
length slot ``uci_socket_write`` uses for a maximum payload -- for the
length) and refuses an explicit address inside the routine.

The end-to-end tests stage input the way a direct caller would (input
first, then the routine), run the image in the 6502 interpreter from
``test_uci_turbo_fence_register``, and assert the ``$DF1D`` command stream.

Evidence grade: interpreter model only; nothing here has run on a device.
"""
from __future__ import annotations

import pytest

from c64_test_harness import uci_network as u
from c64_test_harness.memory_policy import HARNESS_SCRATCH
from c64_test_harness.uci_network import (
    NET_CMD_SOCKET_WRITE,
    SOCKET_WRITE_MAX_BYTES,
    TARGET_NETWORK,
    _CODE_ADDR,
    build_socket_write,
)
from test_uci_turbo_fence_register import _UciMachine  # same-directory helper

#: Where a direct caller must stage input for the default build.  Literal on
#: purpose: the scratch-table tests below tie the module constants to these.
PLAIN = {"socket_id": 0xC100, "data": 0xC101, "len": 0xC1FF}
TURBO = {"socket_id": 0xC403, "data": 0xC500,
         "len": 0xC500 + SOCKET_WRITE_MAX_BYTES}


def _run_default_build(turbo: bool, payload: bytes, socket_id: int = 0x2A):
    slots = TURBO if turbo else PLAIN
    code = build_socket_write(turbo_safe=turbo)
    cpu = _UciMachine(b"", data=b"")
    # Stage the input first, as a caller must, then upload the routine.
    cpu.mem[slots["socket_id"]] = socket_id
    cpu.mem[slots["data"]:slots["data"] + len(payload)] = payload
    n = len(payload)
    cpu.mem[slots["len"]] = n & 0xFF
    cpu.mem[slots["len"] + 1] = (n >> 8) & 0xFF
    cpu.mem[_CODE_ADDR:_CODE_ADDR + len(code)] = code
    cpu.run()
    return cpu, code


def _payload(n: int) -> bytes:
    return bytes((0x41 + i) % 0x100 for i in range(n))


@pytest.mark.parametrize("n", [0, 1, 17, 200])
@pytest.mark.parametrize("turbo", [False, True], ids=["plain", "turbo"])
def test_default_build_sends_the_staged_socket_id_and_payload(
    turbo: bool, n: int
) -> None:
    payload = _payload(n)
    cpu, _ = _run_default_build(turbo, payload)
    assert cpu.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_WRITE, 0x2A,
                             *payload]


@pytest.mark.parametrize("n", [255, 256, 600, SOCKET_WRITE_MAX_BYTES])
def test_turbo_default_build_sends_payloads_past_one_page(n: int) -> None:
    """Turbo only: the plain default buffer ``$C101-$C1FE`` holds 254 bytes."""
    payload = _payload(n)
    cpu, _ = _run_default_build(True, payload)
    assert cpu.cmd_bytes == [TARGET_NETWORK, NET_CMD_SOCKET_WRITE, 0x2A,
                             *payload]


@pytest.mark.parametrize("turbo", [False, True], ids=["plain", "turbo"])
def test_no_default_input_byte_lies_inside_the_routine(turbo: bool) -> None:
    slots = TURBO if turbo else PLAIN
    code = build_socket_write(turbo_safe=turbo)
    lo, hi = _CODE_ADDR, _CODE_ADDR + len(code)
    inputs = [slots["socket_id"], slots["data"], slots["len"], slots["len"] + 1]
    inside = [hex(a) for a in inputs if lo <= a < hi]
    assert not inside, f"inside ${lo:04X}-${hi - 1:04X}: {inside}"


class TestTheGuard:
    def test_socket_id_inside_the_routine_is_refused(self) -> None:
        with pytest.raises(ValueError, match="socket_id_addr"):
            build_socket_write(0xC100, TURBO["data"], TURBO["len"],
                               turbo_safe=True)

    def test_data_inside_the_routine_is_refused(self) -> None:
        with pytest.raises(ValueError, match="data_addr"):
            build_socket_write(TURBO["socket_id"], 0xC101, TURBO["len"],
                               turbo_safe=True)

    def test_length_inside_the_routine_is_refused(self) -> None:
        # $C1FF (the plain default) is NOT inside the 446 B turbo routine
        # ($C000-$C1BD); $C100 is.
        with pytest.raises(ValueError, match="data_len_addr"):
            build_socket_write(TURBO["socket_id"], TURBO["data"], 0xC100,
                               turbo_safe=True)

    def test_length_low_byte_alone_inside_the_routine_is_refused(self) -> None:
        """Low byte on the routine's last byte, high byte just past it: only
        the low-byte check can catch this."""
        n = len(build_socket_write(turbo_safe=True))
        with pytest.raises(ValueError, match=r"data_len_addr=\$"):
            build_socket_write(TURBO["socket_id"], TURBO["data"],
                               _CODE_ADDR + n - 1, turbo_safe=True)

    def test_length_high_byte_inside_the_routine_is_refused(self) -> None:
        """The length is two bytes: ``code_addr - 1`` puts the high byte at
        the routine's first byte."""
        with pytest.raises(ValueError, match="data_len_addr"):
            build_socket_write(TURBO["socket_id"], TURBO["data"],
                               _CODE_ADDR - 1, turbo_safe=True)

    def test_first_routine_byte_is_refused(self) -> None:
        with pytest.raises(ValueError):
            build_socket_write(_CODE_ADDR, TURBO["data"], TURBO["len"],
                               turbo_safe=True)

    def test_last_routine_byte_is_refused(self) -> None:
        n = len(build_socket_write(turbo_safe=True))
        with pytest.raises(ValueError):
            build_socket_write(_CODE_ADDR + n - 1, TURBO["data"], TURBO["len"],
                               turbo_safe=True)

    def test_one_past_the_routine_is_accepted(self) -> None:
        n = len(build_socket_write(turbo_safe=True))
        build_socket_write(_CODE_ADDR + n, TURBO["data"], TURBO["len"],
                           turbo_safe=True)

    def test_length_just_before_the_routine_start_minus_one_is_accepted(self) -> None:
        """``code_addr - 2`` keeps both length bytes below the routine."""
        build_socket_write(TURBO["socket_id"], TURBO["data"], _CODE_ADDR - 2,
                           turbo_safe=True)


class TestPlainIsUnchanged:
    def test_plain_default_equals_the_explicit_legacy_slots(self) -> None:
        assert build_socket_write() == build_socket_write(
            PLAIN["socket_id"], PLAIN["data"], PLAIN["len"])

    def test_helper_path_is_unchanged(self) -> None:
        """``uci_socket_write`` passes its slots explicitly; its turbo build
        with an in-bounds address still builds."""
        build_socket_write(0xC403, 0xC500, 0xC500 + 5, turbo_safe=True)


class TestTheTurboSlotsMatchTheScratchTable:
    @staticmethod
    def _covering(addr: int, length: int, owner: str):
        return [r for r in HARNESS_SCRATCH
                if r.start <= addr and addr + length <= r.end
                and owner in r.owner]

    def test_constants_are_the_slots_this_test_stages(self) -> None:
        assert (u._TURBO_SOCKET_ID_ADDR, u._TURBO_WRITE_DATA_ADDR,
                u._TURBO_WRITE_LEN_ADDR) == (
            TURBO["socket_id"], TURBO["data"], TURBO["len"])

    def test_socket_id_row_names_build_socket_write(self) -> None:
        rows = self._covering(u._TURBO_SOCKET_ID_ADDR, 1, "build_socket_write")
        assert len(rows) == 1 and rows[0].length == 1

    def test_data_and_length_row_names_build_socket_write(self) -> None:
        span = u._TURBO_WRITE_LEN_ADDR + 2 - u._TURBO_WRITE_DATA_ADDR
        rows = self._covering(u._TURBO_WRITE_DATA_ADDR, span, "build_socket_write")
        assert len(rows) == 1
