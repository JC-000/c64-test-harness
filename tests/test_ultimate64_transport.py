"""Unit tests for Ultimate64Transport — the client is mocked, no network."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64 import Ultimate64Transport
from c64_test_harness.transport import C64Transport


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.read_mem.return_value = b""
    return client


@pytest.fixture
def transport(mock_client: MagicMock) -> Ultimate64Transport:
    return Ultimate64Transport(host="192.0.2.1", client=mock_client)


def test_properties(transport: Ultimate64Transport) -> None:
    assert transport.screen_cols == 40
    assert transport.screen_rows == 25


def test_custom_dimensions(mock_client: MagicMock) -> None:
    t = Ultimate64Transport(host="h", client=mock_client, cols=80, rows=50)
    assert t.screen_cols == 80
    assert t.screen_rows == 50


def test_protocol_conformance(transport: Ultimate64Transport) -> None:
    assert isinstance(transport, C64Transport)


def test_client_property_returns_underlying_client(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    assert transport.client is mock_client
    assert transport.client is transport._client


def test_read_memory_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = b"\x01\x02\x03"
    result = transport.read_memory(0x1000, 3)
    mock_client.read_mem.assert_called_once_with(0x1000, 3)
    assert result == b"\x01\x02\x03"


def test_read_memory_zero_length(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    assert transport.read_memory(0x1000, 0) == b""
    mock_client.read_mem.assert_not_called()


def test_write_memory_bytes(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, b"\xaa\xbb")
    mock_client.write_mem.assert_called_once_with(0x2000, b"\xaa\xbb")


def test_write_memory_list_converted(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, [0x01, 0x02, 0x03])
    mock_client.write_mem.assert_called_once_with(0x2000, b"\x01\x02\x03")


def test_write_memory_empty_noop(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.write_memory(0x2000, b"")
    transport.write_memory(0x2000, [])
    mock_client.write_mem.assert_not_called()


def test_read_screen_codes(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = bytes(i % 256 for i in range(1000))
    codes = transport.read_screen_codes()
    mock_client.read_mem.assert_called_once_with(0x0400, 1000)
    assert len(codes) == 1000
    assert all(isinstance(c, int) for c in codes)


def test_read_screen_codes_custom_base(mock_client: MagicMock) -> None:
    mock_client.read_mem.return_value = bytes(1000)
    t = Ultimate64Transport(host="h", client=mock_client, screen_base=0x8400)
    t.read_screen_codes()
    mock_client.read_mem.assert_called_once_with(0x8400, 1000)


def test_inject_keys_empty(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.inject_keys([])
    mock_client.read_mem.assert_not_called()
    mock_client.write_mem.assert_not_called()


def test_inject_keys_simple(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    # Current count reads back as 0.
    mock_client.read_mem.return_value = b"\x00"
    transport.inject_keys([0x41, 0x42, 0x43])

    # Reads count once.
    assert mock_client.read_mem.call_count == 1
    mock_client.read_mem.assert_called_with(0x00C6, 1)
    # Writes buffer bytes, then count byte.
    assert mock_client.write_mem.call_count == 2
    calls = mock_client.write_mem.call_args_list
    assert calls[0].args == (0x0277, b"\x41\x42\x43")
    assert calls[1].args == (0x00C6, bytes([3]))


def test_inject_keys_waits_for_full_drain_before_writing(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    """A partially-filled buffer is never topped up at an offset.

    The KERNAL shifts the buffer down and decrements $C6 concurrently
    with our DMA writes, so writing at (buf + count) races it.  The
    transport must wait for $C6 == 0 and always write at offset 0.
    """
    # Existing count is 4 on the first poll, drained on the second.
    mock_client.read_mem.side_effect = [b"\x04", b"\x00"]
    transport.inject_keys([0x11, 0x22, 0x33])
    calls = mock_client.write_mem.call_args_list
    # No write happened while $C6 was non-zero; the buffer write starts
    # at offset 0 and the count is exactly the chunk length.
    assert calls[0].args == (0x0277, b"\x11\x22\x33")
    assert calls[1].args == (0x00C6, bytes([3]))
    assert mock_client.read_mem.call_count == 2


def test_inject_keys_chunks_when_over_max(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # First poll: count=0, second poll: count=0 again (drained), etc.
    mock_client.read_mem.return_value = b"\x00"
    keys = list(range(12))  # 12 keys — 10 + 2
    transport.inject_keys(keys)
    # Two chunks.
    write_calls = [c for c in mock_client.write_mem.call_args_list]
    assert len(write_calls) == 4  # 2 * (buffer, count)
    assert write_calls[0].args == (0x0277, bytes(range(10)))
    assert write_calls[1].args == (0x00C6, bytes([10]))
    assert write_calls[2].args == (0x0277, bytes([10, 11]))
    assert write_calls[3].args == (0x00C6, bytes([2]))


def test_inject_keys_waits_for_drain(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Simulate buffer full for 2 polls, then drained.
    mock_client.read_mem.side_effect = [b"\x0a", b"\x0a", b"\x00"]
    transport.inject_keys([0x01])
    # Should have polled 3 times.
    assert mock_client.read_mem.call_count == 3
    # Single write pair after drain.
    assert mock_client.write_mem.call_count == 2


# ---------------------------------------------------------------------------
# inject_joystick — active-high protocol convention onto active-low CIA
# ---------------------------------------------------------------------------


def test_inject_joystick_port2_inverts_and_writes_dc00(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Active-high fire (bit 4) → CIA active-low byte with bit 4 cleared.
    transport.inject_joystick(2, 0x10)
    mock_client.write_mem.assert_called_once_with(0xDC00, bytes([0x10 ^ 0x1F]))


def test_inject_joystick_port1_inverts_and_writes_dc01(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Up + fire pressed (bits 0 and 4) → bits 0/4 low, bits 1-3 high.
    transport.inject_joystick(1, 0b1_0001)
    mock_client.write_mem.assert_called_once_with(0xDC01, bytes([0b0_1110]))


def test_inject_joystick_nothing_pressed_all_lines_high(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    transport.inject_joystick(2, 0x00)
    mock_client.write_mem.assert_called_once_with(0xDC00, bytes([0x1F]))


def test_inject_joystick_bits_5_to_7_pass_through(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    # Only the five joystick bits are inverted.
    transport.inject_joystick(2, 0xE0)
    mock_client.write_mem.assert_called_once_with(0xDC00, bytes([0xE0 | 0x1F]))


def test_inject_joystick_invalid_port(transport: Ultimate64Transport) -> None:
    with pytest.raises(ValueError, match="port"):
        transport.inject_joystick(3, 0x10)


def test_inject_joystick_value_out_of_range(
    transport: Ultimate64Transport,
) -> None:
    with pytest.raises(ValueError, match="range"):
        transport.inject_joystick(1, 0x100)


def test_inject_joystick_is_policy_visible(
    mock_client: MagicMock, caplog: pytest.LogCaptureFixture
) -> None:
    """The CIA write routes through write_memory, not client.write_mem.

    With a policy that reserves the CIA range the write still succeeds
    (CIA registers are I/O, not consumer RAM — the per-call
    ``override="inject-joystick"`` bypasses the check by design), but
    the bypass is logged at WARNING so the policy sees every write.
    """
    from c64_test_harness import MemoryPolicy, MemoryRegion

    policy = MemoryPolicy(
        reserved_regions=(MemoryRegion(0xDC00, 0xDD00, "CIA1"),),
    )
    t = Ultimate64Transport(host="h", client=mock_client, memory_policy=policy)
    with caplog.at_level("WARNING"):
        t.inject_joystick(2, 0x10)
    # The write went through the policy-checked path and was logged.
    assert any("inject-joystick" in r.message for r in caplog.records)
    mock_client.write_mem.assert_called_once_with(0xDC00, bytes([0x10 ^ 0x1F]))


def test_read_registers_removed_from_protocol() -> None:
    """``read_registers`` is intentionally NOT part of ``C64Transport``.

    CPU-register inspection is a VICE-only power (binary monitor).
    The U64 REST API cannot honour it, so the protocol was narrowed
    rather than left as a silent-NotImplementedError trap.  Callers
    that need registers should depend on ``BinaryViceTransport``
    directly.  This test guards against accidental re-introduction.
    """
    assert "read_registers" not in C64Transport.__dict__
    # Likewise the U64 transport must not advertise it.
    assert not hasattr(Ultimate64Transport, "read_registers")


def test_read_palette_returns_vic_palette(transport: Ultimate64Transport) -> None:
    """``read_palette`` returns the canonical 16-entry VIC-II palette."""
    from c64_test_harness.backends.u64_video_capture import VIC_PALETTE

    palette = transport.read_palette()
    assert isinstance(palette, list)
    assert len(palette) == 16
    # Every entry is an (r, g, b) tuple of ints in 0..255.
    for entry in palette:
        assert isinstance(entry, tuple)
        assert len(entry) == 3
        for chan in entry:
            assert isinstance(chan, int)
            assert 0 <= chan <= 255
    # Matches the source-of-truth constant.
    assert palette == [tuple(rgb) for rgb in VIC_PALETTE]


def test_read_palette_matches_vice_shape(transport: Ultimate64Transport) -> None:
    """Return type must satisfy ``list[tuple[int, int, int]]`` — same
    as ``BinaryViceTransport.read_palette``."""
    palette = transport.read_palette()
    assert palette[0] == (0x00, 0x00, 0x00)   # black
    assert palette[1] == (0xFF, 0xFF, 0xFF)   # white


def test_resume_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.resume()
    mock_client.resume.assert_called_once_with()


def test_close_delegates(transport: Ultimate64Transport, mock_client: MagicMock) -> None:
    transport.close()
    mock_client.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# set_speed / get_speed — wrap ultimate64_helpers.{set,get}_turbo_*
# ---------------------------------------------------------------------------


def test_set_speed_1_calls_set_turbo_mhz_none(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake_set(_client: object, mhz: object) -> None:
        calls.append(mhz)

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.set_turbo_mhz", fake_set
    )
    transport.set_speed(1)
    assert calls == [None]


def test_set_speed_none_calls_set_turbo_mhz_48(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake_set(_client: object, mhz: object) -> None:
        calls.append(mhz)

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.set_turbo_mhz", fake_set
    )
    transport.set_speed(None)
    assert calls == [48]


@pytest.mark.parametrize("mhz", [2, 4, 8, 12, 48])
def test_set_speed_supported_int_forwards(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch, mhz: int
) -> None:
    calls: list[object] = []

    def fake_set(_client: object, m: object) -> None:
        calls.append(m)

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.set_turbo_mhz", fake_set
    )
    transport.set_speed(mhz)
    assert calls == [mhz]


def test_set_speed_unsupported_int_raises(
    transport: Ultimate64Transport,
) -> None:
    # set_turbo_mhz validates against the device enum and raises
    # ValueError; we should surface that for unsupported multipliers
    # like 7, 9, 11, etc.
    with pytest.raises(ValueError):
        transport.set_speed(7)


_U64E_CPU_SPEED_PRESETS = [
    " 1", " 2", " 3", " 4", " 5", " 6", " 8", "10",
    "12", "14", "16", "20", "24", "32", "40", "48",
]
_C64U_CPU_SPEED_PRESETS = [
    " 1", " 2", " 3", " 4", " 6", " 8", "10",
    "12", "14", "16", "20", "24", "32", "40", "48", "64",
]


def _cpu_speed_item_response(presets: list[str]) -> dict:
    return {
        "U64 Specific Settings": {
            "CPU Speed": {
                "current": " 1",
                "presets": list(presets),
                "default": " 1",
            },
        },
        "errors": [],
    }


def test_set_speed_none_uses_probed_max_on_c64u(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    """C64U fw 1.1.0 has a '64' CPU-Speed step — None means 64, not 48."""
    mock_client.get_config_item.return_value = _cpu_speed_item_response(
        _C64U_CPU_SPEED_PRESETS
    )
    transport.set_speed(None)
    mock_client.set_config_items.assert_called_once_with(
        "U64 Specific Settings",
        {"CPU Speed": "64", "Turbo Control": "Manual"},
    )


def test_set_speed_none_uses_probed_max_on_u64e(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    mock_client.get_config_item.return_value = _cpu_speed_item_response(
        _U64E_CPU_SPEED_PRESETS
    )
    transport.set_speed(None)
    mock_client.set_config_items.assert_called_once_with(
        "U64 Specific Settings",
        {"CPU Speed": "48", "Turbo Control": "Manual"},
    )


def test_set_speed_generation_foreign_5_raises_locally_on_c64u(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    mock_client.get_config_item.return_value = _cpu_speed_item_response(
        _C64U_CPU_SPEED_PRESETS
    )
    with pytest.raises(ValueError, match="device generation"):
        transport.set_speed(5)
    mock_client.set_config_items.assert_not_called()


def test_set_speed_generation_foreign_64_raises_locally_on_u64e(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    mock_client.get_config_item.return_value = _cpu_speed_item_response(
        _U64E_CPU_SPEED_PRESETS
    )
    with pytest.raises(ValueError, match="device generation"):
        transport.set_speed(64)
    mock_client.set_config_items.assert_not_called()


def test_set_speed_64_goes_on_the_wire_on_c64u(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    mock_client.get_config_item.return_value = _cpu_speed_item_response(
        _C64U_CPU_SPEED_PRESETS
    )
    transport.set_speed(64)
    mock_client.set_config_items.assert_called_once_with(
        "U64 Specific Settings",
        {"CPU Speed": "64", "Turbo Control": "Manual"},
    )


def test_get_speed_native_when_turbo_off(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_enabled",
        lambda _c: False,
    )
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_mhz",
        lambda _c: None,
    )
    assert transport.get_speed() == 1


def test_get_speed_returns_mhz_when_turbo_on(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_enabled",
        lambda _c: True,
    )
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_mhz",
        lambda _c: 8,
    )
    assert transport.get_speed() == 8


def test_get_speed_none_when_turbo_on_but_mhz_unknown(
    transport: Ultimate64Transport, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_enabled",
        lambda _c: True,
    )
    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64_helpers.get_turbo_mhz",
        lambda _c: None,
    )
    assert transport.get_speed() is None


# ---------------------------------------------------------------------------
# reset(scope=...) — wraps client.{reset,reboot,drive_reset}
# ---------------------------------------------------------------------------


def test_reset_cpu_calls_client_reset(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    transport.reset("cpu")
    mock_client.reset.assert_called_once_with()
    mock_client.reboot.assert_not_called()
    mock_client.drive_reset.assert_not_called()


def test_reset_default_scope_is_cpu(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    transport.reset()
    mock_client.reset.assert_called_once_with()


def test_reset_machine_calls_client_reboot(
    transport: Ultimate64Transport, mock_client: MagicMock
) -> None:
    transport.reset("machine")
    mock_client.reboot.assert_called_once_with()
    mock_client.reset.assert_not_called()


@pytest.mark.parametrize("slot", ["a", "b", "A", "B"])
def test_reset_drive_string_slot(
    transport: Ultimate64Transport, mock_client: MagicMock, slot: str
) -> None:
    transport.reset("drive", drive=slot)
    mock_client.drive_reset.assert_called_once_with(slot.lower())


@pytest.mark.parametrize("idx,slot", [(0, "a"), (1, "b")])
def test_reset_drive_int_index(
    transport: Ultimate64Transport, mock_client: MagicMock, idx: int, slot: str
) -> None:
    transport.reset("drive", drive=idx)
    mock_client.drive_reset.assert_called_once_with(slot)


def test_reset_drive_requires_drive(transport: Ultimate64Transport) -> None:
    with pytest.raises(ValueError, match="drive"):
        transport.reset("drive")


def test_reset_drive_invalid_string(transport: Ultimate64Transport) -> None:
    with pytest.raises(ValueError, match="drive"):
        transport.reset("drive", drive="c")


def test_reset_drive_int_out_of_range(transport: Ultimate64Transport) -> None:
    with pytest.raises(ValueError, match="drive"):
        transport.reset("drive", drive=2)


def test_reset_drive_bool_refused(transport: Ultimate64Transport) -> None:
    # bool is int subclass; refuse the silent coercion.
    with pytest.raises(ValueError, match="bool"):
        transport.reset("drive", drive=True)


def test_reset_unknown_scope(transport: Ultimate64Transport) -> None:
    with pytest.raises(ValueError, match="scope"):
        transport.reset("nuke")


def test_constructs_own_client_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    created: dict = {}

    class FakeClient:
        def __init__(self, **kwargs: object) -> None:
            created.update(kwargs)

    monkeypatch.setattr(
        "c64_test_harness.backends.ultimate64.Ultimate64Client",
        FakeClient,
    )
    Ultimate64Transport(host="10.0.0.5", password="pw", port=8080, timeout=3.0)
    assert created == {
        "host": "10.0.0.5",
        "password": "pw",
        "port": 8080,
        "timeout": 3.0,
    }
