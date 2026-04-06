"""Live read-only integration tests for Ultimate64Client.

Gated by the ``U64_HOST`` env var — e.g.:

    U64_HOST=192.168.1.81 python3 -m pytest tests/test_ultimate64_client_live.py -v

Only READ-ONLY endpoints are exercised. No resets, no mounts, no config
writes. The device on the LAN is shared.
"""
from __future__ import annotations

import os

import pytest

from c64_test_harness.backends.device_lock import DeviceLock
from c64_test_harness.backends.ultimate64_client import Ultimate64Client

_HOST = os.environ.get("U64_HOST")
_PW = os.environ.get("U64_PASSWORD")

pytestmark = pytest.mark.skipif(
    not _HOST,
    reason="U64_HOST not set — live Ultimate device tests disabled",
)


@pytest.fixture(scope="module")
def client() -> Ultimate64Client:
    lock = DeviceLock(_HOST)
    if not lock.acquire(timeout=120.0):
        pytest.skip(f"Could not acquire device lock for {_HOST}")
    yield Ultimate64Client(_HOST, password=_PW, timeout=8.0)
    lock.release()


def test_get_version(client: Ultimate64Client) -> None:
    v = client.get_version()
    assert isinstance(v, dict)
    # "version" key seen in live probe; fall back to any non-empty dict
    assert v, "empty /v1/version response"


def test_get_info(client: Ultimate64Client) -> None:
    info = client.get_info()
    assert isinstance(info, dict)
    assert isinstance(info.get("product"), str)
    assert info["product"], "product field is empty"
    assert isinstance(info.get("firmware_version"), str)
    # errors array should be present and empty
    assert info.get("errors", []) == []


def test_list_configs_contains_expected_categories(client: Ultimate64Client) -> None:
    cats = client.list_configs()
    assert isinstance(cats, list)
    assert len(cats) >= 10
    assert "U64 Specific Settings" in cats
    assert "C64 and Cartridge Settings" in cats


def test_get_u64_specific_settings(client: Ultimate64Client) -> None:
    resp = client.get_config_category("U64 Specific Settings")
    assert isinstance(resp, dict)
    assert "U64 Specific Settings" in resp
    assert resp.get("errors", []) == []
    inner = resp["U64 Specific Settings"]
    assert isinstance(inner, dict)
    assert "CPU Speed" in inner


def test_cpu_speed_enum_preserves_leading_space(client: Ultimate64Client) -> None:
    resp = client.get_config_item("U64 Specific Settings", "CPU Speed")
    assert resp.get("errors", []) == []
    item = resp["U64 Specific Settings"]["CPU Speed"]
    assert isinstance(item, dict)
    values = item.get("values")
    assert isinstance(values, list)
    # Leading space on single-digit values is significant
    assert " 1" in values, f"expected ' 1' in CPU Speed values, got {values!r}"
    # Max value expected to be a 2-char string
    for v in values:
        assert isinstance(v, str)
        assert len(v) == 2


def test_reu_size_enum_present(client: Ultimate64Client) -> None:
    resp = client.get_config_item("C64 and Cartridge Settings", "REU Size")
    assert resp.get("errors", []) == []
    item = resp["C64 and Cartridge Settings"]["REU Size"]
    values = item.get("values")
    assert isinstance(values, list)
    assert "512 KB" in values
    assert "16 MB" in values


def test_list_drives(client: Ultimate64Client) -> None:
    drives = client.list_drives()
    assert isinstance(drives, dict)
    assert "drives" in drives
    assert isinstance(drives["drives"], list)
    assert drives["drives"], "no drives returned"
    # Expect at least one entry keyed "a"
    slot_names: set[str] = set()
    for entry in drives["drives"]:
        assert isinstance(entry, dict)
        slot_names.update(entry.keys())
    assert "a" in slot_names
