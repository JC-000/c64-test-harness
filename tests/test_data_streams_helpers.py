"""Unit tests for Data Streams config helpers in ultimate64_helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from c64_test_harness.backends.ultimate64_helpers import (
    CAT_DATA_STREAMS,
    DEBUG_MODE_6510,
    DEBUG_MODE_VIC,
    DEBUG_MODE_6510_VIC,
    DEBUG_MODE_1541,
    DEBUG_MODE_6510_1541,
    DEBUG_MODES,
    get_data_streams_config,
    set_stream_destination,
    get_debug_stream_mode,
    set_debug_stream_mode,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _mock_client(config_data: dict | None = None) -> MagicMock:
    """Build a mock Ultimate64Client with canned config response."""
    client = MagicMock()
    if config_data is None:
        config_data = {
            CAT_DATA_STREAMS: {
                "Stream VIC to": "239.0.1.64:11000",
                "Stream Audio to": "239.0.1.65:11001",
                "Stream Debug to": "239.0.1.66:11002",
                "Debug Stream Mode": "6510 Only",
            },
            "errors": [],
        }
    client.get_config_category.return_value = config_data
    return client


# ---------------------------------------------------------------------------
# DEBUG_MODES constant
# ---------------------------------------------------------------------------

class TestDebugModes:
    def test_contains_all_modes(self) -> None:
        assert len(DEBUG_MODES) == 5

    def test_6510_only(self) -> None:
        assert DEBUG_MODE_6510 == "6510 Only"
        assert DEBUG_MODE_6510 in DEBUG_MODES

    def test_vic_only(self) -> None:
        assert DEBUG_MODE_VIC == "VIC Only"
        assert DEBUG_MODE_VIC in DEBUG_MODES

    def test_6510_vic(self) -> None:
        assert DEBUG_MODE_6510_VIC == "6510 & VIC"
        assert DEBUG_MODE_6510_VIC in DEBUG_MODES

    def test_1541_only(self) -> None:
        assert DEBUG_MODE_1541 == "1541 Only"
        assert DEBUG_MODE_1541 in DEBUG_MODES

    def test_6510_1541(self) -> None:
        assert DEBUG_MODE_6510_1541 == "6510 & 1541"
        assert DEBUG_MODE_6510_1541 in DEBUG_MODES


# ---------------------------------------------------------------------------
# get_data_streams_config
# ---------------------------------------------------------------------------

class TestGetDataStreamsConfig:
    def test_returns_dict(self) -> None:
        client = _mock_client()
        result = get_data_streams_config(client)
        assert isinstance(result, dict)
        client.get_config_category.assert_called_once_with(CAT_DATA_STREAMS)

    def test_contains_stream_keys(self) -> None:
        client = _mock_client()
        result = get_data_streams_config(client)
        assert "Stream VIC to" in result
        assert "Stream Audio to" in result
        assert "Stream Debug to" in result
        assert "Debug Stream Mode" in result


# ---------------------------------------------------------------------------
# set_stream_destination
# ---------------------------------------------------------------------------

class TestSetStreamDestination:
    def test_set_video(self) -> None:
        client = _mock_client()
        set_stream_destination(client, "video", "10.0.0.1:11000")
        client.set_config_items.assert_called_once_with(
            CAT_DATA_STREAMS, {"Stream VIC to": "10.0.0.1:11000"},
        )

    def test_set_audio(self) -> None:
        client = _mock_client()
        set_stream_destination(client, "audio", "10.0.0.1:11001")
        client.set_config_items.assert_called_once_with(
            CAT_DATA_STREAMS, {"Stream Audio to": "10.0.0.1:11001"},
        )

    def test_set_debug(self) -> None:
        client = _mock_client()
        set_stream_destination(client, "debug", "10.0.0.1:11002")
        client.set_config_items.assert_called_once_with(
            CAT_DATA_STREAMS, {"Stream Debug to": "10.0.0.1:11002"},
        )

    def test_unknown_type_raises(self) -> None:
        client = _mock_client()
        with pytest.raises(ValueError, match="Unknown stream_type"):
            set_stream_destination(client, "midi", "10.0.0.1:12000")


# ---------------------------------------------------------------------------
# get/set debug stream mode
# ---------------------------------------------------------------------------

class TestDebugStreamMode:
    def test_get_mode(self) -> None:
        client = _mock_client()
        mode = get_debug_stream_mode(client)
        assert mode == "6510 Only"

    def test_set_valid_mode(self) -> None:
        client = _mock_client()
        set_debug_stream_mode(client, DEBUG_MODE_6510_VIC)
        client.set_config_items.assert_called_once_with(
            CAT_DATA_STREAMS, {"Debug Stream Mode": "6510 & VIC"},
        )

    def test_set_all_modes(self) -> None:
        for mode in DEBUG_MODES:
            client = _mock_client()
            set_debug_stream_mode(client, mode)
            client.set_config_items.assert_called_once()

    def test_set_invalid_mode_raises(self) -> None:
        client = _mock_client()
        with pytest.raises(ValueError, match="Unknown debug stream mode"):
            set_debug_stream_mode(client, "Both CPUs")


class TestGetDebugStreamMode:
    def test_reads_from_config(self) -> None:
        client = _mock_client()
        mode = get_debug_stream_mode(client)
        assert mode == "6510 Only"
        client.get_config_category.assert_called_with(CAT_DATA_STREAMS)
