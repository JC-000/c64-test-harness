"""Tests for labels.py — VICE label file parsing."""

import pytest

from c64_test_harness.labels import Labels


class TestLabels:
    def test_from_file(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert len(labels) > 0

    def test_known_labels(self, labels_path):
        labels = Labels.from_file(labels_path)
        # key_data should be at $40AB (from the actual labels file)
        assert labels.address("key_data") == 0x40AB
        assert labels.address("iv_data") == 0x409B
        assert labels.address("expanded_key") == 0x40EB

    def test_reverse_lookup(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert labels.name(0x40AB) == "key_data"

    def test_getitem(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert labels["key_data"] == 0x40AB

    def test_getitem_missing_raises(self, labels_path):
        labels = Labels.from_file(labels_path)
        with pytest.raises(KeyError):
            labels["nonexistent_label"]

    def test_contains(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert "key_data" in labels
        assert "nonexistent" not in labels

    def test_address_not_found(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert labels.address("zzz_not_a_label") is None

    def test_name_not_found(self, labels_path):
        labels = Labels.from_file(labels_path)
        assert labels.name(0xDEAD) is None

    def test_many_labels_loaded(self, labels_path):
        labels = Labels.from_file(labels_path)
        # The file has ~760 lines, many are labels
        assert len(labels) > 100

    def test_repr(self, labels_path):
        labels = Labels.from_file(labels_path)
        r = repr(labels)
        assert "Labels(" in r
        assert "entries" in r


class TestNonCLabels:
    """ld65 emits address-space-neutral ``al XXXXXX .name`` lines
    (no ``C:`` prefix) for labels outside the 16-bit C64 code space —
    e.g. REU offsets. The parser must accept both forms."""

    def _write(self, tmp_path, lines: list[str]):
        p = tmp_path / "labels.txt"
        p.write_text("\n".join(lines) + "\n")
        return p

    def test_non_c_line_parsed(self, tmp_path):
        path = self._write(
            tmp_path,
            ["al 022100 .REU_OVERLAY_X25519"],
        )
        labels = Labels.from_file(path)
        assert labels["REU_OVERLAY_X25519"] == 0x022100

    def test_non_c_line_reverse_lookup(self, tmp_path):
        path = self._write(
            tmp_path,
            ["al 024100 .REU_OVERLAY_P384"],
        )
        labels = Labels.from_file(path)
        assert labels.name(0x024100) == "REU_OVERLAY_P384"

    def test_mixed_c_and_non_c(self, tmp_path):
        path = self._write(
            tmp_path,
            [
                "al C:4200 .__CRYPTO_OVERLAY_START__",
                "al 020100 .REU_OVERLAY_X25519",
                "al 022100 .REU_OVERLAY_P256",
                "al C:C000 .tcp_recv_buf",
            ],
        )
        labels = Labels.from_file(path)
        assert len(labels) == 4
        assert labels["__CRYPTO_OVERLAY_START__"] == 0x4200
        assert labels["tcp_recv_buf"] == 0xC000
        assert labels["REU_OVERLAY_X25519"] == 0x020100
        assert labels["REU_OVERLAY_P256"] == 0x022100

    def test_address_above_64k(self, tmp_path):
        """Non-C addresses frequently exceed 16 bits."""
        path = self._write(
            tmp_path,
            ["al 040000 .REU_P384_PRECOMPUTE_BASE"],
        )
        labels = Labels.from_file(path)
        assert labels["REU_P384_PRECOMPUTE_BASE"] == 0x40000
        assert labels["REU_P384_PRECOMPUTE_BASE"] > 0xFFFF
