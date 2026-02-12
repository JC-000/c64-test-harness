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
