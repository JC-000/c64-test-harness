"""Tests for package-level ``__version__`` attribute."""

import re

import c64_test_harness


def test_version_exposed():
    assert hasattr(c64_test_harness, "__version__")
    assert isinstance(c64_test_harness.__version__, str)
    # Either a real version like "0.11.2" or the fallback "0+unknown"
    assert (
        re.match(r"^\d+\.\d+", c64_test_harness.__version__)
        or c64_test_harness.__version__ == "0+unknown"
    )


def test_version_in_all():
    assert "__version__" in c64_test_harness.__all__
