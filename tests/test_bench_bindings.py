"""Device-free tests of scripts/_bench_bindings.py: the local file that says which device each bench script may drive.

The ids are fake; the bench's real ones live only in the owner's local bench.toml.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts"))
import _bench_bindings as bb  # noqa: E402

GOOD = """\
[devices.abc123]
jtag_adapter_location = "9-9.9"
jtag_adapter_baud = 921600

[devices.DEF456]
ram_run = true
jtag_adapter_location = "9-9.9"
jtag_adapter_baud = 921600
jtag_scan_only = true
outlet = "http://192.0.2.200"
outlet_kind = "shelly-gen2"
"""


@pytest.fixture
def bench(tmp_path, monkeypatch):
    p = tmp_path / "bench.toml"
    monkeypatch.setenv(bb.BENCH_FILE_ENV, str(p))
    return p


def test_a_bound_device_is_read_in_either_case(bench):
    bench.write_text(GOOD)
    assert bb.bench_table("ABC123") == bb.bench_table("abc123") == {"jtag_adapter_location": "9-9.9",
                                                                     "jtag_adapter_baud": 921600}
    assert bb.bench_table("def456") == {"ram_run": True, "jtag_adapter_location": "9-9.9", "jtag_adapter_baud": 921600,
                                        "jtag_scan_only": True, "outlet": "http://192.0.2.200",
                                        "outlet_kind": "shelly-gen2"}


def test_the_volatile_baseline_keys_are_read(bench):
    """volatile_load.py's baseline: an image path, its SHA-256 and the git hash it reports, per device."""
    table = {"volatile_baseline_image": "/images/base.u64", "volatile_baseline_sha256": "AB" * 32,
             "volatile_baseline_git_hash": "0123abc", "volatile_baseline_bit": "/images/base.bit",
             "volatile_baseline_bit_sha256": "cd" * 32, "volatile_baseline_jtag_image": "/images/base.elf",
             "volatile_baseline_jtag_sha256": "ef" * 32}
    bench.write_text("[devices.abc123]\n" + "".join(f'{k} = "{v}"\n' for k, v in table.items()))
    assert bb.bench_table("abc123") == table


def test_an_unbound_device_has_no_table(bench):
    bench.write_text(GOOD)
    assert bb.bench_table("fed789") == {}


def test_a_missing_file_is_refused_and_named(bench):
    with pytest.raises(bb.BenchBindingError, match="does not exist") as e:
        bb.bench_table("abc123")
    assert str(bench) in str(e.value)


@pytest.mark.parametrize("text", [
    '[devices.abc123]\nram_run = "true"\n',
    "[devices.abc123]\nram_run = 1\n",
    "[devices.abc123]\njtag_scan_only = 0\n",
    '[devices.abc123]\njtag_adapter_baud = "921600"\n',
    "[devices.abc123]\njtag_adapter_baud = true\n",
    "[devices.abc123]\njtag_adapter_baud = 0\n",
    "[devices.abc123]\njtag_adapter_location = 12\n",
    '[devices.abc123]\njtag_adapter_location = " "\n',
    '[devices.abc123]\noutlet = ""\n',
    "[devices.abc123]\noutlet_kind = 2\n",
    '[devices.abc123]\nvolatile_baseline_image = ""\n',
    '[devices.abc123]\nvolatile_baseline_sha256 = "abc123"\n',
    '[devices.abc123]\nvolatile_baseline_git_hash = "main"\n',
    "[devices.abc123]\nvolatile_baseline_bit = 7\n",
    f'[devices.abc123]\nvolatile_baseline_bit_sha256 = "{"g" * 64}"\n',
    f'[devices.abc123]\nvolatile_baseline_jtag_sha256 = "{"0" * 63}"\n',
], ids=["ram-run-str", "ram-run-int", "scan-only-int", "baud-str", "baud-bool", "baud-zero", "location-int",
        "location-blank", "outlet-empty", "kind-int", "baseline-image-empty", "baseline-sha-short",
        "baseline-hash-not-hex", "baseline-bit-int", "baseline-bit-sha-not-hex", "baseline-jtag-sha-short"])
def test_a_value_of_the_wrong_type_is_refused(bench, text):
    bench.write_text(text)
    with pytest.raises(bb.BenchBindingError, match="must be"):
        bb.bench_table("fed789")                    # any lookup: the whole file is validated


@pytest.mark.parametrize("text", [
    '[devices.abc123]\nhosts = ["192.0.2.26"]\n',          # an alias-map key belongs in devices.toml
    "[devices.abc123]\nramrun = true\n",                    # a typo
    "ram_run = true\n",                                     # outside any device table
    '[device.abc123]\nram_run = true\n',
    '[devices."ab c"]\nram_run = true\n',
    "[devices.abc123]\nram_run = true\n[devices.ABC123]\nram_run = false\n",
    "[devices]\nabc123 = 1\n",
    "[devices.abc123\n",
], ids=["hosts", "typo-key", "top-level-key", "typo-table", "bad-id", "two-tables", "not-a-table", "bad-toml"])
def test_a_malformed_file_is_refused(bench, text):
    bench.write_text(text)
    with pytest.raises(bb.BenchBindingError):
        bb.bench_table("fed789")


def test_the_default_path_follows_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.delenv(bb.BENCH_FILE_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    p = tmp_path / "c64-test-harness" / "bench.toml"
    assert bb.bench_file_path() == p
    p.parent.mkdir()
    p.write_text(GOOD)
    assert bb.bench_table("abc123")["jtag_adapter_location"] == "9-9.9"


def test_an_explicit_path_wins_over_the_environment(bench, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text(GOOD)
    assert bb.bench_table("abc123", other)["jtag_adapter_baud"] == 921600
