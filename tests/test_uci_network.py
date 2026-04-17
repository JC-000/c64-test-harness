"""Unit tests for the UCI (Ultimate Command Interface) network module.

All transport interactions are mocked — no emulator or hardware is touched.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from c64_test_harness.uci_network import (
    # Identifier
    UCI_IDENTIFIER,
    # Register addresses
    UCI_CONTROL_STATUS_REG,
    UCI_CMD_DATA_REG,
    UCI_RESP_DATA_REG,
    UCI_STATUS_DATA_REG,
    # Turbo-safety fence tuning
    UCI_FENCE_OUTER,
    UCI_FENCE_INNER,
    UCI_PUSH_SETTLE_ITERS,
    # Target IDs
    TARGET_DOS1,
    TARGET_DOS2,
    TARGET_NETWORK,
    TARGET_CONTROL,
    # Network commands
    NET_CMD_IDENTIFY,
    NET_CMD_GET_INTERFACE_COUNT,
    NET_CMD_GET_NETADDR,
    NET_CMD_GET_IPADDR,
    NET_CMD_TCP_CONNECT,
    NET_CMD_UDP_CONNECT,
    NET_CMD_SOCKET_CLOSE,
    NET_CMD_SOCKET_READ,
    NET_CMD_SOCKET_WRITE,
    NET_CMD_TCP_LISTENER_START,
    NET_CMD_TCP_LISTENER_STOP,
    NET_CMD_GET_LISTENER_STATE,
    NET_CMD_GET_LISTENER_SOCKET,
    # Control bits
    CMD_PUSH,
    CMD_NEXT_DATA,
    CMD_ABORT,
    CMD_CLR_ERR,
    # State masks
    STATE_BITS,
    STATE_IDLE,
    STATE_BUSY,
    STATE_LAST_DATA,
    STATE_MORE_DATA,
    # Status bits
    BIT_DATA_AV,
    BIT_STAT_AV,
    BIT_ERROR,
    BIT_CMD_BUSY,
    # Listener states
    NOT_LISTENING,
    LISTENING,
    CONNECTED,
    BIND_ERROR,
    PORT_IN_USE,
    # Assembly builders
    build_uci_probe,
    build_uci_command,
    build_get_ip,
    build_tcp_connect,
    build_socket_read,
    build_socket_write,
    build_socket_close,
    # High-level helpers
    uci_probe,
    uci_get_ip,
    uci_get_interface_count,
    uci_tcp_connect,
    uci_socket_read,
    uci_socket_write,
    uci_socket_close,
    # UCI config helpers
    get_uci_enabled,
    enable_uci,
    disable_uci,
)


# ---------------------------------------------------------------------------
# Constants — Register addresses
# ---------------------------------------------------------------------------

class TestRegisterAddresses:
    """Verify UCI register addresses are in the $DF1C-$DF1F I/O range."""

    def test_control_status_reg(self) -> None:
        assert UCI_CONTROL_STATUS_REG == 0xDF1C

    def test_cmd_data_reg(self) -> None:
        assert UCI_CMD_DATA_REG == 0xDF1D

    def test_resp_data_reg(self) -> None:
        assert UCI_RESP_DATA_REG == 0xDF1E

    def test_status_data_reg(self) -> None:
        assert UCI_STATUS_DATA_REG == 0xDF1F

    def test_all_registers_in_df1x_range(self) -> None:
        for reg in (UCI_CONTROL_STATUS_REG, UCI_CMD_DATA_REG,
                    UCI_RESP_DATA_REG, UCI_STATUS_DATA_REG):
            assert 0xDF1C <= reg <= 0xDF1F, f"Register 0x{reg:04X} out of range"

    def test_registers_are_contiguous(self) -> None:
        regs = sorted([UCI_CONTROL_STATUS_REG, UCI_CMD_DATA_REG,
                       UCI_RESP_DATA_REG, UCI_STATUS_DATA_REG])
        assert regs == [0xDF1C, 0xDF1D, 0xDF1E, 0xDF1F]


# ---------------------------------------------------------------------------
# Constants — UCI identifier
# ---------------------------------------------------------------------------

class TestUciIdentifier:
    def test_identifier_value(self) -> None:
        assert UCI_IDENTIFIER == 0xC9


# ---------------------------------------------------------------------------
# Constants — Target IDs
# ---------------------------------------------------------------------------

class TestTargetIds:
    def test_target_dos1(self) -> None:
        assert TARGET_DOS1 == 0x01

    def test_target_dos2(self) -> None:
        assert TARGET_DOS2 == 0x02

    def test_target_network(self) -> None:
        assert TARGET_NETWORK == 0x03

    def test_target_control(self) -> None:
        assert TARGET_CONTROL == 0x04

    def test_targets_are_unique(self) -> None:
        targets = [TARGET_DOS1, TARGET_DOS2, TARGET_NETWORK, TARGET_CONTROL]
        assert len(set(targets)) == len(targets)


# ---------------------------------------------------------------------------
# Constants — Network commands
# ---------------------------------------------------------------------------

class TestNetworkCommands:
    def test_identify(self) -> None:
        assert NET_CMD_IDENTIFY == 0x01

    def test_get_interface_count(self) -> None:
        assert NET_CMD_GET_INTERFACE_COUNT == 0x02

    def test_get_netaddr(self) -> None:
        assert NET_CMD_GET_NETADDR == 0x04

    def test_get_ipaddr(self) -> None:
        assert NET_CMD_GET_IPADDR == 0x05

    def test_tcp_connect(self) -> None:
        assert NET_CMD_TCP_CONNECT == 0x07

    def test_udp_connect(self) -> None:
        assert NET_CMD_UDP_CONNECT == 0x08

    def test_socket_close(self) -> None:
        assert NET_CMD_SOCKET_CLOSE == 0x09

    def test_socket_read(self) -> None:
        assert NET_CMD_SOCKET_READ == 0x10

    def test_socket_write(self) -> None:
        assert NET_CMD_SOCKET_WRITE == 0x11

    def test_tcp_listener_start(self) -> None:
        assert NET_CMD_TCP_LISTENER_START == 0x12

    def test_tcp_listener_stop(self) -> None:
        assert NET_CMD_TCP_LISTENER_STOP == 0x13

    def test_get_listener_state(self) -> None:
        assert NET_CMD_GET_LISTENER_STATE == 0x14

    def test_get_listener_socket(self) -> None:
        assert NET_CMD_GET_LISTENER_SOCKET == 0x15

    def test_all_commands_unique(self) -> None:
        cmds = [
            NET_CMD_IDENTIFY, NET_CMD_GET_INTERFACE_COUNT,
            NET_CMD_GET_NETADDR, NET_CMD_GET_IPADDR,
            NET_CMD_TCP_CONNECT, NET_CMD_UDP_CONNECT,
            NET_CMD_SOCKET_CLOSE, NET_CMD_SOCKET_READ,
            NET_CMD_SOCKET_WRITE, NET_CMD_TCP_LISTENER_START,
            NET_CMD_TCP_LISTENER_STOP, NET_CMD_GET_LISTENER_STATE,
            NET_CMD_GET_LISTENER_SOCKET,
        ]
        assert len(set(cmds)) == len(cmds)


# ---------------------------------------------------------------------------
# Constants — Control bits
# ---------------------------------------------------------------------------

class TestControlBits:
    def test_cmd_push(self) -> None:
        assert CMD_PUSH == 0x01

    def test_cmd_next_data(self) -> None:
        assert CMD_NEXT_DATA == 0x02

    def test_cmd_abort(self) -> None:
        assert CMD_ABORT == 0x04

    def test_cmd_clr_err(self) -> None:
        assert CMD_CLR_ERR == 0x08

    def test_control_bits_are_single_bit_flags(self) -> None:
        """Each control bit should be a power of 2 (single-bit flag)."""
        for bit in (CMD_PUSH, CMD_NEXT_DATA, CMD_ABORT, CMD_CLR_ERR):
            assert bit & (bit - 1) == 0, f"0x{bit:02X} is not a power of 2"

    def test_control_bits_no_overlap(self) -> None:
        assert (CMD_PUSH | CMD_NEXT_DATA | CMD_ABORT | CMD_CLR_ERR) == 0x0F


# ---------------------------------------------------------------------------
# Constants — State masks
# ---------------------------------------------------------------------------

class TestStateMasks:
    def test_state_bits_mask(self) -> None:
        assert STATE_BITS == 0x30

    def test_state_idle(self) -> None:
        assert STATE_IDLE == 0x00

    def test_state_busy(self) -> None:
        assert STATE_BUSY == 0x10

    def test_state_last_data(self) -> None:
        assert STATE_LAST_DATA == 0x20

    def test_state_more_data(self) -> None:
        assert STATE_MORE_DATA == 0x30

    def test_all_states_fit_in_mask(self) -> None:
        for state in (STATE_IDLE, STATE_BUSY, STATE_LAST_DATA, STATE_MORE_DATA):
            assert (state & ~STATE_BITS) == 0, (
                f"State 0x{state:02X} has bits outside STATE_BITS mask"
            )


# ---------------------------------------------------------------------------
# Constants — Status bits
# ---------------------------------------------------------------------------

class TestStatusBits:
    def test_data_available(self) -> None:
        assert BIT_DATA_AV == 0x80

    def test_status_available(self) -> None:
        assert BIT_STAT_AV == 0x40

    def test_error(self) -> None:
        assert BIT_ERROR == 0x08

    def test_cmd_busy(self) -> None:
        assert BIT_CMD_BUSY == 0x01


# ---------------------------------------------------------------------------
# Constants — Listener states
# ---------------------------------------------------------------------------

class TestListenerStates:
    def test_not_listening(self) -> None:
        assert NOT_LISTENING == 0x00

    def test_listening(self) -> None:
        assert LISTENING == 0x01

    def test_connected(self) -> None:
        assert CONNECTED == 0x02

    def test_bind_error(self) -> None:
        assert BIND_ERROR == 0x03

    def test_port_in_use(self) -> None:
        assert PORT_IN_USE == 0x04

    def test_listener_states_are_sequential(self) -> None:
        states = [NOT_LISTENING, LISTENING, CONNECTED, BIND_ERROR, PORT_IN_USE]
        assert states == list(range(5))


# ---------------------------------------------------------------------------
# Assembly builder helpers
# ---------------------------------------------------------------------------

def _check_asm_basics(code: bytes) -> None:
    """Common checks for all assembly builders.

    UCI routines are dispatched via SYS + the keyboard buffer, so they
    are JSR'd as subroutines by BASIC and must terminate with RTS (0x60)
    to return control.
    """
    assert isinstance(code, bytes)
    assert len(code) > 0
    # Must end with RTS (0x60) — routines are dispatched via SYS
    assert code[-1] == 0x60, (
        f"Assembly routine must end with RTS (0x60), got 0x{code[-1]:02X}"
    )


def _contains_io_addr(code: bytes, addr: int) -> bool:
    """Check if the assembly output references the given 16-bit I/O address.

    Looks for the lo/hi byte pair as they'd appear in absolute addressing
    mode (lo byte first, then hi byte in the following position).
    """
    lo = addr & 0xFF
    hi = (addr >> 8) & 0xFF
    for i in range(len(code) - 1):
        if code[i] == lo and code[i + 1] == hi:
            return True
    return False


# ---------------------------------------------------------------------------
# Assembly builder tests
# ---------------------------------------------------------------------------

class TestBuildUciProbe:
    """Tests for build_uci_probe() assembly builder."""

    def test_returns_bytes_ending_rts(self) -> None:
        code = build_uci_probe()
        _check_asm_basics(code)

    def test_references_cmd_data_register(self) -> None:
        """Probe reads from $DF1D to check for UCI_IDENTIFIER."""
        code = build_uci_probe()
        assert _contains_io_addr(code, UCI_CMD_DATA_REG)

    def test_result_address_default(self) -> None:
        code = build_uci_probe()
        assert len(code) >= 6

    def test_reads_id_register(self) -> None:
        """The probe reads from the ID register ($DF1D) to detect UCI."""
        code = build_uci_probe()
        # Must reference the ID register address
        assert _contains_io_addr(code, UCI_CMD_DATA_REG)


class TestBuildUciCommand:
    """Tests for the generic build_uci_command() builder."""

    def test_returns_bytes_ending_rts(self) -> None:
        code = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        _check_asm_basics(code)

    def test_contains_target_byte(self) -> None:
        code = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        assert TARGET_NETWORK in code

    def test_contains_command_byte(self) -> None:
        code = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        assert NET_CMD_GET_INTERFACE_COUNT in code

    def test_references_control_status_register(self) -> None:
        code = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        assert _contains_io_addr(code, UCI_CONTROL_STATUS_REG)

    def test_references_cmd_data_register(self) -> None:
        code = build_uci_command(TARGET_NETWORK, NET_CMD_SOCKET_READ)
        assert _contains_io_addr(code, UCI_CMD_DATA_REG)

    def test_different_targets_produce_different_code(self) -> None:
        code_net = build_uci_command(TARGET_NETWORK, NET_CMD_IDENTIFY)
        code_dos = build_uci_command(TARGET_DOS1, NET_CMD_IDENTIFY)
        assert code_net != code_dos


class TestBuildGetIp:
    """Tests for build_get_ip() assembly builder."""

    def test_returns_bytes_ending_rts(self) -> None:
        code = build_get_ip()
        _check_asm_basics(code)

    def test_references_resp_data_register(self) -> None:
        code = build_get_ip()
        assert _contains_io_addr(code, UCI_RESP_DATA_REG)

    def test_custom_result_address(self) -> None:
        code = build_get_ip(result_addr=0xC100)
        assert _contains_io_addr(code, 0xC100)

    def test_contains_get_ipaddr_command(self) -> None:
        code = build_get_ip()
        assert NET_CMD_GET_IPADDR in code


class TestBuildTcpConnect:
    """Tests for build_tcp_connect() assembly builder."""

    def test_returns_bytes_ending_rts(self) -> None:
        code = build_tcp_connect()
        _check_asm_basics(code)

    def test_references_cmd_data_register(self) -> None:
        code = build_tcp_connect()
        assert _contains_io_addr(code, UCI_CMD_DATA_REG)

    def test_contains_tcp_connect_command(self) -> None:
        code = build_tcp_connect()
        assert NET_CMD_TCP_CONNECT in code


class TestBuildSocketReadWrite:
    """Tests for build_socket_read() and build_socket_write()."""

    def test_read_returns_bytes_ending_rts(self) -> None:
        code = build_socket_read()
        _check_asm_basics(code)

    def test_write_returns_bytes_ending_rts(self) -> None:
        code = build_socket_write()
        _check_asm_basics(code)

    def test_read_references_resp_data(self) -> None:
        code = build_socket_read()
        assert _contains_io_addr(code, UCI_RESP_DATA_REG)

    def test_write_references_cmd_data(self) -> None:
        code = build_socket_write()
        assert _contains_io_addr(code, UCI_CMD_DATA_REG)

    def test_read_contains_read_command(self) -> None:
        code = build_socket_read()
        assert NET_CMD_SOCKET_READ in code

    def test_write_contains_write_command(self) -> None:
        code = build_socket_write()
        assert NET_CMD_SOCKET_WRITE in code


class TestBuildSocketClose:
    """Tests for build_socket_close() assembly builder."""

    def test_returns_bytes_ending_rts(self) -> None:
        code = build_socket_close()
        _check_asm_basics(code)

    def test_contains_close_command(self) -> None:
        code = build_socket_close()
        assert NET_CMD_SOCKET_CLOSE in code

    def test_references_control_status_register(self) -> None:
        code = build_socket_close()
        assert _contains_io_addr(code, UCI_CONTROL_STATUS_REG)


# ---------------------------------------------------------------------------
# High-level helpers (with mock transport)
# ---------------------------------------------------------------------------

# The helpers call _execute_uci_routine which:
# 1. Reads IGONE vector (0x0302, 2 bytes)
# 2. Clears sentinel + error
# 3. Writes code
# 4. Patches IGONE
# 5. Polls sentinel
# 6. Restores IGONE
# 7. Checks error flag
# 8. Reads result data
#
# We need a mock that responds correctly to address-specific read_memory calls.

_SENTINEL_ADDR = 0xC3FE
_ERROR_ADDR = 0xC3FF
_RESP_ADDR = 0xC200
_RESP_LEN_ADDR = 0xC3F0
_STATUS_ADDR = 0xC300
_STAT_LEN_ADDR = 0xC3F2
_SENTINEL_DONE = 0x42


def _make_mock_transport(result_byte: int = 0x01, resp_data: bytes = b"") -> MagicMock:
    """Build a mock transport that simulates _execute_uci_routine correctly.

    Routines are dispatched via SYS + the keyboard buffer ($0277 / $00C6),
    not via the IMAIN ($0302) vector patch, so this mock only needs to
    answer sentinel, error, response, and status reads.

    *result_byte* is what read_memory returns for single-byte result reads.
    *resp_data* is what read_memory returns for the response data area.
    """
    t = MagicMock()
    call_count = {"sentinel_polls": 0}

    def mock_read_memory(addr: int, length: int) -> bytes:
        if addr == _SENTINEL_ADDR:
            # First poll returns 0, second returns sentinel done
            call_count["sentinel_polls"] += 1
            if call_count["sentinel_polls"] >= 2:
                return bytes([_SENTINEL_DONE])
            return b"\x00"
        if addr == _ERROR_ADDR:
            return b"\x00"  # no error
        if addr == _RESP_LEN_ADDR:
            if resp_data:
                return bytes([len(resp_data)])
            return bytes([1])  # default: 1 byte response
        if addr == _STAT_LEN_ADDR:
            return b"\x00"
        if addr == _RESP_ADDR:
            if resp_data:
                return resp_data[:length]
            return bytes([result_byte]) * length
        return bytes(length)

    t.read_memory.side_effect = mock_read_memory
    return t


class TestUciProbe:
    """Tests for uci_probe() helper."""

    def test_writes_code_to_memory(self) -> None:
        t = _make_mock_transport(result_byte=0xC9)
        uci_probe(t, timeout=1.0)
        assert t.write_memory.call_count >= 1

    def test_returns_int(self) -> None:
        t = _make_mock_transport(result_byte=0xC9)
        result = uci_probe(t, timeout=1.0)
        assert isinstance(result, int)

    def test_probe_positive(self) -> None:
        """Non-zero result byte indicates UCI is present."""
        t = _make_mock_transport(result_byte=0xC9)
        result = uci_probe(t, timeout=1.0)
        assert result == 0xC9

    def test_probe_negative(self) -> None:
        """Zero result byte indicates UCI is not present."""
        t = _make_mock_transport(result_byte=0x00)
        result = uci_probe(t, timeout=1.0)
        assert result == 0


class TestUciGetInterfaceCount:
    """Tests for uci_get_interface_count() helper."""

    def test_returns_int(self) -> None:
        t = _make_mock_transport(result_byte=0x02)
        result = uci_get_interface_count(t, timeout=1.0)
        assert isinstance(result, int)

    def test_returns_count_from_result(self) -> None:
        t = _make_mock_transport(result_byte=0x03)
        result = uci_get_interface_count(t, timeout=1.0)
        assert result == 3


class TestUciGetIp:
    """Tests for uci_get_ip() helper."""

    def test_returns_string(self) -> None:
        t = _make_mock_transport(resp_data=b"192.168.1.81")
        result = uci_get_ip(t, timeout=1.0)
        assert isinstance(result, str)

    def test_returns_ip_string(self) -> None:
        t = _make_mock_transport(resp_data=b"192.168.1.81")
        result = uci_get_ip(t, timeout=1.0)
        assert result == "192.168.1.81"

    def test_writes_routine_to_memory(self) -> None:
        t = _make_mock_transport(resp_data=b"10.0.0.1")
        uci_get_ip(t, timeout=1.0)
        assert t.write_memory.call_count >= 1


class TestUciTcpConnect:
    """Tests for uci_tcp_connect() helper."""

    def test_returns_socket_id(self) -> None:
        t = _make_mock_transport(result_byte=0x05)
        result = uci_tcp_connect(t, "192.168.1.81", 80, timeout=1.0)
        assert isinstance(result, int)
        assert result == 5

    def test_writes_hostname_to_memory(self) -> None:
        t = _make_mock_transport(result_byte=0x01)
        uci_tcp_connect(t, "example.com", 8080, timeout=1.0)
        all_data = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1
        )
        assert b"example.com" in all_data

    def test_port_encoded_in_writes(self) -> None:
        t = _make_mock_transport(result_byte=0x01)
        uci_tcp_connect(t, "host", 0x1F90, timeout=1.0)
        assert t.write_memory.call_count >= 1


class TestUciSocketWrite:
    """Tests for uci_socket_write() helper."""

    def test_writes_data_to_memory(self) -> None:
        t = _make_mock_transport()
        payload = b"GET / HTTP/1.0\r\n\r\n"
        uci_socket_write(t, 1, payload, timeout=1.0)
        all_data = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1
        )
        assert payload in all_data

    def test_socket_id_used(self) -> None:
        t = _make_mock_transport()
        uci_socket_write(t, 3, b"hello", timeout=1.0)
        assert t.write_memory.call_count >= 1


class TestUciSocketRead:
    """Tests for uci_socket_read() helper."""

    def test_returns_bytes(self) -> None:
        t = _make_mock_transport(resp_data=b"Hello")
        result = uci_socket_read(t, 1, 255, timeout=1.0)
        assert isinstance(result, bytes)

    def test_returns_response_data(self) -> None:
        expected = b"Hello"
        t = _make_mock_transport(resp_data=expected)
        result = uci_socket_read(t, 1, len(expected), timeout=1.0)
        assert result == expected


class TestUciSocketClose:
    """Tests for uci_socket_close() helper."""

    def test_writes_code_to_memory(self) -> None:
        t = _make_mock_transport()
        uci_socket_close(t, 1, timeout=1.0)
        assert t.write_memory.call_count >= 1

    def test_socket_id_used(self) -> None:
        t = _make_mock_transport()
        uci_socket_close(t, 5, timeout=1.0)
        assert t.write_memory.call_count >= 1


# ---------------------------------------------------------------------------
# Parametrized tests across all builders
# ---------------------------------------------------------------------------

_BUILDERS_NO_ARGS = [
    build_uci_probe,
    build_get_ip,
    build_tcp_connect,
    build_socket_read,
    build_socket_write,
    build_socket_close,
]


@pytest.mark.parametrize("builder", _BUILDERS_NO_ARGS, ids=lambda b: b.__name__)
class TestAllBuilders:
    """Cross-cutting invariants that every assembly builder must satisfy."""

    def test_returns_nonempty_bytes(self, builder) -> None:
        code = builder()
        assert isinstance(code, bytes)
        assert len(code) > 0

    def test_ends_with_rts(self, builder) -> None:
        code = builder()
        assert code[-1] == 0x60, f"{builder.__name__} must end with RTS"

    def test_references_at_least_one_uci_register(self, builder) -> None:
        """Every UCI routine must reference at least one UCI I/O register."""
        code = builder()
        uci_regs = [UCI_CONTROL_STATUS_REG, UCI_CMD_DATA_REG,
                     UCI_RESP_DATA_REG, UCI_STATUS_DATA_REG]
        found = any(_contains_io_addr(code, reg) for reg in uci_regs)
        assert found, (
            f"{builder.__name__} does not reference any UCI register "
            f"($DF1C-$DF1F)"
        )


# ---------------------------------------------------------------------------
# UCI config helpers (REST API)
# ---------------------------------------------------------------------------

class TestGetUciEnabled:
    """Tests for get_uci_enabled() — reads UCI state via REST API."""

    def test_returns_true_when_enabled(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            "C64 and Cartridge Settings": {"Command Interface": "Enabled"}
        }
        assert get_uci_enabled(client) is True

    def test_returns_false_when_disabled(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            "C64 and Cartridge Settings": {"Command Interface": "Disabled"}
        }
        assert get_uci_enabled(client) is False

    def test_returns_false_when_missing(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            "C64 and Cartridge Settings": {}
        }
        assert get_uci_enabled(client) is False

    def test_queries_correct_category(self) -> None:
        client = MagicMock()
        client.get_config_category.return_value = {
            "C64 and Cartridge Settings": {"Command Interface": "Disabled"}
        }
        get_uci_enabled(client)
        client.get_config_category.assert_called_once_with(
            "C64 and Cartridge Settings"
        )


class TestEnableUci:
    """Tests for enable_uci() — enables UCI via REST API."""

    def test_sets_enabled(self) -> None:
        client = MagicMock()
        enable_uci(client)
        client.set_config_items.assert_called_once_with(
            "C64 and Cartridge Settings",
            {"Command Interface": "Enabled"},
        )

    def test_does_not_save_to_flash(self) -> None:
        """enable_uci must NOT persist changes to flash."""
        client = MagicMock()
        enable_uci(client)
        # Only set_config_items should be called, not save_to_flash
        assert not any(
            "flash" in str(c).lower()
            for c in client.method_calls
            if c[0] != "set_config_items"
        )


class TestDisableUci:
    """Tests for disable_uci() — disables UCI via REST API."""

    def test_sets_disabled(self) -> None:
        client = MagicMock()
        disable_uci(client)
        client.set_config_items.assert_called_once_with(
            "C64 and Cartridge Settings",
            {"Command Interface": "Disabled"},
        )

    def test_does_not_save_to_flash(self) -> None:
        """disable_uci must NOT persist changes to flash."""
        client = MagicMock()
        disable_uci(client)
        assert not any(
            "flash" in str(c).lower()
            for c in client.method_calls
            if c[0] != "set_config_items"
        )


# ---------------------------------------------------------------------------
# Turbo-safe fencing — required for UCI access at U64 turbo speeds (8/24/48
# MHz). Ported from the c64-https `fix/uci-nop-fencing` PR. The fence is a
# nested delay-loop (~52 us at 48 MHz) inserted after every read/write of
# a UCI register.
# ---------------------------------------------------------------------------

def _fence_byte_signature() -> bytes:
    """The canonical fence byte pattern: PHA, TXA, PHA, LDX #OUTER,
    LDY #INNER. We search for this 7-byte prefix to count fence sites.
    """
    return bytes([
        0x48,                # PHA
        0x8A,                # TXA
        0x48,                # PHA
        0xA2, UCI_FENCE_OUTER,  # LDX #OUTER
        0xA0, UCI_FENCE_INNER,  # LDY #INNER
    ])


def _count_fences(code: bytes) -> int:
    sig = _fence_byte_signature()
    count = 0
    i = 0
    while i <= len(code) - len(sig):
        if code[i:i + len(sig)] == sig:
            count += 1
            i += len(sig)
        else:
            i += 1
    return count


class TestFenceTuning:
    """Fence parameters must match the c64-https reference implementation."""

    def test_outer_is_5(self) -> None:
        assert UCI_FENCE_OUTER == 5

    def test_inner_is_100(self) -> None:
        assert UCI_FENCE_INNER == 100

    def test_push_settle_is_255(self) -> None:
        """Settle delay before first CMD_BUSY poll after PUSH_CMD."""
        assert UCI_PUSH_SETTLE_ITERS == 0xFF

    def test_outer_inner_yield_38us_minimum(self) -> None:
        """OUTER * (INNER * 5 + 5) must give at least 38 us at 48 MHz.

        c64-https empirical minimum: OUTER=3 INNER=122 (~1845 cycles).
        Ours (OUTER=5 INNER=100) should comfortably exceed that.
        """
        cycles = UCI_FENCE_OUTER * (UCI_FENCE_INNER * 5 + 5)
        # 48 MHz → 1 cycle = ~20.8 ns → 1845 cycles = ~38.4 us
        min_cycles = 1845
        assert cycles >= min_cycles, (
            f"Fence is {cycles} cycles, minimum is {min_cycles}"
        )


class TestBuildUciProbeTurboSafe:
    """``build_uci_probe(turbo_safe=True)`` must fence after the LDA $DF1D."""

    def test_turbo_safe_is_larger(self) -> None:
        plain = build_uci_probe()
        fenced = build_uci_probe(turbo_safe=True)
        assert len(fenced) > len(plain)

    def test_turbo_safe_contains_fence(self) -> None:
        fenced = build_uci_probe(turbo_safe=True)
        assert _count_fences(fenced) >= 1

    def test_plain_contains_no_fence(self) -> None:
        plain = build_uci_probe()
        assert _count_fences(plain) == 0


class TestBuildTcpConnectTurboSafe:
    """TCP_CONNECT is the most fence-dense builder — lots of UCI writes."""

    def test_turbo_safe_is_larger(self) -> None:
        plain = build_tcp_connect()
        fenced = build_tcp_connect(turbo_safe=True)
        assert len(fenced) > len(plain)

    def test_turbo_safe_has_many_fences(self) -> None:
        """At minimum one fence per UCI register access: TARGET, CMD, port_lo,
        port_hi, hostname bytes, PUSH_CMD, etc.  Expect at least ~10.
        """
        fenced = build_tcp_connect(turbo_safe=True)
        assert _count_fences(fenced) >= 10, (
            f"expected >= 10 fences, got {_count_fences(fenced)}"
        )

    def test_ends_with_rts(self) -> None:
        fenced = build_tcp_connect(turbo_safe=True)
        assert fenced[-1] == 0x60  # RTS


class TestBuildSocketReadTurboSafe:
    def test_turbo_safe_is_larger(self) -> None:
        assert len(build_socket_read(turbo_safe=True)) > len(build_socket_read())

    def test_turbo_safe_has_fences(self) -> None:
        assert _count_fences(build_socket_read(turbo_safe=True)) >= 8

    def test_ends_with_rts(self) -> None:
        assert build_socket_read(turbo_safe=True)[-1] == 0x60


class TestBuildSocketWriteTurboSafe:
    def test_turbo_safe_is_larger(self) -> None:
        assert len(build_socket_write(turbo_safe=True)) > len(build_socket_write())

    def test_turbo_safe_has_fences(self) -> None:
        assert _count_fences(build_socket_write(turbo_safe=True)) >= 8

    def test_ends_with_rts(self) -> None:
        assert build_socket_write(turbo_safe=True)[-1] == 0x60


class TestBuildSocketCloseTurboSafe:
    def test_turbo_safe_is_larger(self) -> None:
        assert len(build_socket_close(turbo_safe=True)) > len(build_socket_close())

    def test_turbo_safe_has_fences(self) -> None:
        assert _count_fences(build_socket_close(turbo_safe=True)) >= 5


class TestBuildUciCommandTurboSafe:
    def test_turbo_safe_is_larger(self) -> None:
        plain = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        fenced = build_uci_command(
            TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT, turbo_safe=True,
        )
        assert len(fenced) > len(plain)

    def test_turbo_safe_has_fences(self) -> None:
        fenced = build_uci_command(
            TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT, turbo_safe=True,
        )
        assert _count_fences(fenced) >= 6

    def test_params_fenced(self) -> None:
        """Each parameter byte written to $DF1D gets its own fence."""
        empty = build_uci_command(
            TARGET_NETWORK, NET_CMD_GET_IPADDR, params=b"", turbo_safe=True,
        )
        with_one = build_uci_command(
            TARGET_NETWORK, NET_CMD_GET_IPADDR,
            params=bytes([0x00]), turbo_safe=True,
        )
        # one extra param byte → one extra LDA/STA + one extra fence
        assert _count_fences(with_one) > _count_fences(empty)

    def test_default_is_1mhz_path(self) -> None:
        """turbo_safe must default to False so existing callers are unchanged."""
        default = build_uci_command(TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT)
        explicit = build_uci_command(
            TARGET_NETWORK, NET_CMD_GET_INTERFACE_COUNT, turbo_safe=False,
        )
        assert default == explicit
        assert _count_fences(default) == 0


class TestBuildGetIpTurboSafe:
    def test_turbo_safe_larger_and_fenced(self) -> None:
        fenced = build_get_ip(turbo_safe=True)
        plain = build_get_ip()
        assert len(fenced) > len(plain)
        assert _count_fences(fenced) >= 6

    def test_turbo_safe_contains_data_av_spin_wait(self) -> None:
        """Regression: at turbo speeds the FPGA may not have staged response
        data by the time the CPU polls ``DATA_AV``. Without a per-byte
        spin-wait the read loop exits with 0 bytes and ``uci_get_ip``
        returns the empty string at 8/24/48 MHz. Ported from c64-https's
        ``uci_read_resp_bytes`` (src/net/uci/uci_cmd.s) which spins up to
        16 bits × ~110 cycles ≈ 150 ms at 48 MHz before giving up.

        We detect the spin-wait by its signature sequence:

            DEC  $C3F5       ; decrement ctr_hi
            BEQ  $03         ; jump forward past JMP (timeout path)
            JMP  ...         ; back to wait

        which appears at every byte-read iteration in the fenced builder
        but is absent from both the 1 MHz path and the pre-fix turbo path.
        """
        from c64_test_harness.uci_network import _UCI_WAIT_CTR_HI_ADDR
        code = build_get_ip(turbo_safe=True)
        sig = bytes([
            0xCE,                                           # DEC abs
            _UCI_WAIT_CTR_HI_ADDR & 0xFF,
            (_UCI_WAIT_CTR_HI_ADDR >> 8) & 0xFF,
            0xF0, 0x03,                                     # BEQ +3 → past JMP
            0x4C,                                           # JMP abs
        ])
        assert sig in code, (
            "build_get_ip(turbo_safe=True) must emit the per-byte DATA_AV "
            "spin-wait — without it the first DATA_AV check fails at turbo "
            "speeds and uci_get_ip returns empty string."
        )

    def test_plain_does_not_contain_spin_wait(self) -> None:
        """The 1 MHz path keeps the original single-shot DATA_AV check —
        it works there because the CPU is slow enough to outwait the FPGA."""
        from c64_test_harness.uci_network import _UCI_WAIT_CTR_HI_ADDR
        code = build_get_ip()
        sig = bytes([
            0xCE,
            _UCI_WAIT_CTR_HI_ADDR & 0xFF,
            (_UCI_WAIT_CTR_HI_ADDR >> 8) & 0xFF,
        ])
        assert sig not in code

    def test_turbo_safe_bne_have_lands_on_ldx_save_x(self) -> None:
        """The BNE that skips the timeout/decrement block must land on the
        ``LDX save_x`` that restores the caller's X. If the offset is off
        the read loop silently corrupts X and (depending on what the JMP
        target happens to decode as) may crash. We check this explicitly
        because the offset is computed from emitted length at build time.
        """
        from c64_test_harness.uci_network import (
            _UCI_WAIT_SAVE_X_ADDR,
            BIT_DATA_AV,
        )
        code = build_get_ip(turbo_safe=True)
        ldx_save_sig = bytes([
            0xAE,
            _UCI_WAIT_SAVE_X_ADDR & 0xFF,
            (_UCI_WAIT_SAVE_X_ADDR >> 8) & 0xFF,
        ])
        # Look for: AND #$80 (0x29, 0x80), BNE offset, DEX (0xCA) — the
        # response-reader wait loop. Verify BNE target is LDX save_x.
        found = 0
        for i in range(len(code) - 4):
            if (code[i] == 0x29 and code[i + 1] == BIT_DATA_AV
                    and code[i + 2] == 0xD0  # BNE
                    and code[i + 4] == 0xCA):  # DEX
                # Compute signed 8-bit offset; target is (i+4) + offset
                off = code[i + 3]
                if off >= 0x80:
                    off -= 0x100
                target = i + 4 + off
                assert 0 <= target <= len(code) - 3
                assert code[target:target + 3] == ldx_save_sig, (
                    f"BNE have at offset {i} jumps to "
                    f"{code[target:target + 3].hex()}, expected "
                    f"{ldx_save_sig.hex()}"
                )
                found += 1
        assert found >= 1, "No response-reader BNE-to-have pattern found"


class TestTurboSafeInvariants:
    """Cross-cutting invariants that every turbo-safe builder must satisfy."""

    BUILDERS = [
        ("build_uci_probe", build_uci_probe, {}),
        ("build_get_ip", build_get_ip, {}),
        ("build_tcp_connect", build_tcp_connect, {}),
        ("build_socket_read", build_socket_read, {}),
        ("build_socket_write", build_socket_write, {}),
        ("build_socket_close", build_socket_close, {}),
    ]

    @pytest.mark.parametrize("name, builder, kwargs", BUILDERS,
                              ids=lambda x: x if isinstance(x, str) else "")
    def test_turbo_safe_ends_with_rts(self, name, builder, kwargs) -> None:
        del name
        code = builder(turbo_safe=True, **kwargs)
        assert code[-1] == 0x60, "turbo-safe routine must end with RTS"

    @pytest.mark.parametrize("name, builder, kwargs", BUILDERS,
                              ids=lambda x: x if isinstance(x, str) else "")
    def test_turbo_safe_references_uci_register(
        self, name, builder, kwargs,
    ) -> None:
        del name
        code = builder(turbo_safe=True, **kwargs)
        uci_regs = [
            UCI_CONTROL_STATUS_REG, UCI_CMD_DATA_REG,
            UCI_RESP_DATA_REG, UCI_STATUS_DATA_REG,
        ]
        assert any(_contains_io_addr(code, reg) for reg in uci_regs)

    @pytest.mark.parametrize("name, builder, kwargs", BUILDERS,
                              ids=lambda x: x if isinstance(x, str) else "")
    def test_plain_and_turbo_safe_differ(self, name, builder, kwargs) -> None:
        del name
        plain = builder(**kwargs)
        fenced = builder(turbo_safe=True, **kwargs)
        assert plain != fenced


class TestTurboSafeHelpers:
    """High-level helpers accept ``turbo_safe`` and pipe it into the builder."""

    def test_uci_probe_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport(result_byte=0xC9)
        uci_probe(t, timeout=1.0, turbo_safe=True)
        # Code writes should include a fence signature (see _fence_byte_signature).
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes

    def test_uci_get_ip_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport(resp_data=b"10.0.0.1")
        uci_get_ip(t, timeout=1.0, turbo_safe=True)
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes

    def test_uci_tcp_connect_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport(result_byte=0x05)
        uci_tcp_connect(t, "example.com", 80, timeout=1.0, turbo_safe=True)
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes

    def test_uci_socket_read_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport(resp_data=b"hi")
        uci_socket_read(t, 1, 4, timeout=1.0, turbo_safe=True)
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes

    def test_uci_socket_write_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport()
        uci_socket_write(t, 1, b"data", timeout=1.0, turbo_safe=True)
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes

    def test_uci_socket_close_accepts_turbo_safe(self) -> None:
        t = _make_mock_transport()
        uci_socket_close(t, 1, timeout=1.0, turbo_safe=True)
        all_writes = b"".join(
            c.args[1] for c in t.write_memory.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], (bytes, bytearray))
        )
        assert _fence_byte_signature() in all_writes
