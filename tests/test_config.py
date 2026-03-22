"""Tests for config.py — TOML and environment variable loading."""

import os
import tempfile

import pytest

from c64_test_harness.config import HarnessConfig


class TestDefaults:
    def test_default_values(self):
        cfg = HarnessConfig()
        assert cfg.backend == "vice"
        assert cfg.vice_host == "127.0.0.1"
        assert cfg.vice_port == 6510
        assert cfg.screen_cols == 40
        assert cfg.screen_rows == 25
        assert cfg.keybuf_addr == 0x0277
        assert cfg.keybuf_max == 10

    def test_poll_interval_defaults(self):
        cfg = HarnessConfig()
        assert cfg.screen_poll_interval == 2.0


class TestFromEnv:
    def test_override_port(self, monkeypatch):
        monkeypatch.setenv("C64TEST_VICE_PORT", "7777")
        cfg = HarnessConfig.from_env()
        assert cfg.vice_port == 7777

    def test_override_bool(self, monkeypatch):
        monkeypatch.setenv("C64TEST_VICE_WARP", "false")
        cfg = HarnessConfig.from_env()
        assert cfg.vice_warp is False

    def test_override_float(self, monkeypatch):
        monkeypatch.setenv("C64TEST_VICE_TIMEOUT", "10.5")
        cfg = HarnessConfig.from_env()
        assert cfg.vice_timeout == 10.5

    def test_override_poll_intervals(self, monkeypatch):
        monkeypatch.setenv("C64TEST_SCREEN_POLL_INTERVAL", "0.5")
        cfg = HarnessConfig.from_env()
        assert cfg.screen_poll_interval == 0.5

    def test_override_string(self, monkeypatch):
        monkeypatch.setenv("C64TEST_VICE_EXECUTABLE", "/usr/local/bin/x64sc")
        cfg = HarnessConfig.from_env()
        assert cfg.vice_executable == "/usr/local/bin/x64sc"

    def test_override_hex_int(self, monkeypatch):
        monkeypatch.setenv("C64TEST_SCREEN_BASE", "0x0800")
        cfg = HarnessConfig.from_env()
        assert cfg.screen_base == 0x0800

    def test_custom_prefix(self, monkeypatch):
        monkeypatch.setenv("MYTEST_VICE_PORT", "8888")
        cfg = HarnessConfig.from_env(prefix="MYTEST_")
        assert cfg.vice_port == 8888

    def test_unset_vars_use_defaults(self):
        cfg = HarnessConfig.from_env()
        assert cfg.vice_port == 6510


class TestFromToml:
    def test_basic_toml(self, tmp_path):
        toml_content = b"""
backend = "vice"

[vice]
port = 7777
host = "10.0.0.1"
warp = false

[screen]
cols = 80
rows = 50
"""
        toml_file = tmp_path / "c64test.toml"
        toml_file.write_bytes(toml_content)
        try:
            cfg = HarnessConfig.from_toml(toml_file)
            assert cfg.backend == "vice"
            assert cfg.vice_port == 7777
            assert cfg.vice_host == "10.0.0.1"
            assert cfg.vice_warp is False
            assert cfg.screen_cols == 80
            assert cfg.screen_rows == 50
        except RuntimeError:
            pytest.skip("TOML support not available (needs Python 3.11+ or tomli)")

    def test_missing_file_raises(self):
        with pytest.raises(FileNotFoundError):
            HarnessConfig.from_toml("/nonexistent/c64test.toml")
