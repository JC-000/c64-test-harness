"""The scratch-address table in docs/memory_safety.md is generated from
``HARNESS_SCRATCH`` (issue #169).  This test fails the suite when the
checked-in table no longer matches the code — run
``scripts/gen_memory_table.py --write`` to refresh it.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "scripts" / "gen_memory_table.py"
_DOC = _REPO / "docs" / "memory_safety.md"


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_memory_table", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_doc_table_matches_generated(gen) -> None:
    text = _DOC.read_text(encoding="utf-8")
    block = gen.extract_block(text)
    assert block is not None, "docs/memory_safety.md lacks the HARNESS_SCRATCH markers"
    assert block == gen.render_block(), (
        "docs/memory_safety.md scratch table has drifted from HARNESS_SCRATCH; "
        "run scripts/gen_memory_table.py --write"
    )


def test_check_mode_agrees_with_test(gen, capsys) -> None:
    assert gen.main(["--check"]) == 0
    assert "up to date" in capsys.readouterr().out


def test_table_has_one_row_per_region(gen) -> None:
    from c64_test_harness import HARNESS_SCRATCH

    rows = [
        line for line in gen.render_table().splitlines()
        if line.startswith("| `$")
    ]
    assert len(rows) == len(HARNESS_SCRATCH)
    for r, line in zip(HARNESS_SCRATCH, rows):
        assert line.startswith(f"| `{r.span}`")
        assert r.owner.split(",")[0].strip() in line


def test_summary_lists_only_non_transient_spans(gen) -> None:
    from c64_test_harness import HARNESS_SCRATCH

    summary = gen.render_summary()
    spans = re.findall(r"`(\$[0-9A-F]{4}(?:-\$[0-9A-F]{4})?)`", summary)
    assert spans == [r.span for r in HARNESS_SCRATCH if not r.transient]
    assert "$0800-$87FF" not in summary


def test_doc_example_ranges_cover_the_code(gen) -> None:
    """The doc's example safe-region `$C000-$CFFF` is a superset the
    consumer grants; it must still contain every `$Cxxx` scratch entry,
    including the `$CF00` stub the old table omitted."""
    from c64_test_harness import HARNESS_SCRATCH

    text = _DOC.read_text(encoding="utf-8")
    assert "$C000-$CFFF" in text
    for r in HARNESS_SCRATCH:
        if 0xC000 <= r.start < 0xD000:
            assert r.end <= 0xD000, r.span
    assert "$CF00" in gen.extract_block(text)
