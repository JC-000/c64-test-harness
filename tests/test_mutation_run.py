"""``scripts/mutation/run.py`` must measure against a green baseline.

Every mutant is scored by running the suite and looking for failures.  A
suite that is *already* red -- one pre-existing failing test -- therefore
kills every mutant, and the kill rate reads as near-perfect while the
suite has measured nothing about any of them.  ``run.py`` used to start
mutating without ever running the unmutated tree once.

These tests drive the baseline check with a stub ``pytest`` so no real
suite (and no VICE) is involved.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import stat
import sys

import pytest

_RUN = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mutation" / "run.py"


@pytest.fixture(scope="module")
def run():
    spec = importlib.util.spec_from_file_location("mutation_run", _RUN)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A minimal tree with the two directories the runner copies."""
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text("def test_a():\n    pass\n")
    return tmp_path


def _stub_pytest(tmp_path: pathlib.Path, body: str) -> str:
    """A fake ``pytest`` binary whose behaviour is *body* (sh)."""
    path = tmp_path / "fake-pytest"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def test_a_red_baseline_aborts_the_run(run, repo, tmp_path):
    fake = _stub_pytest(
        tmp_path, "echo 'FAILED tests/test_x.py::test_a - assert False'; exit 1"
    )
    with pytest.raises(SystemExit) as exc:
        run.check_baseline(repo, tmp_path / "box", fake, ["test_x.py"], timeout=30)
    assert "baseline" in str(exc.value).lower()


def test_a_baseline_that_cannot_collect_aborts_the_run(run, repo, tmp_path):
    """rc=4 is a broken conftest/import: nothing about the mutants is measurable."""
    fake = _stub_pytest(
        tmp_path, "echo 'ImportError while loading conftest'; exit 4"
    )
    with pytest.raises(SystemExit):
        run.check_baseline(repo, tmp_path / "box", fake, ["test_x.py"], timeout=30)


def test_a_green_baseline_lets_the_run_proceed(run, repo, tmp_path):
    fake = _stub_pytest(tmp_path, "echo '1 passed'; exit 0")
    run.check_baseline(repo, tmp_path / "box", fake, ["test_x.py"], timeout=30)


def test_the_baseline_runs_the_unmutated_copy(run, repo, tmp_path):
    """The stub records where it ran: inside the box's ``tests``, on a copy."""
    marker = tmp_path / "cwd.txt"
    fake = _stub_pytest(tmp_path, f"pwd > {marker}; exit 0")
    box = tmp_path / "box"
    run.check_baseline(repo, box, fake, ["test_x.py"], timeout=30)
    ran_in = pathlib.Path(marker.read_text().strip()).resolve()
    assert ran_in == (box / "tests").resolve()
    assert (box / "tests" / "test_x.py").read_text() == (
        repo / "tests" / "test_x.py"
    ).read_text()
    assert os.path.isdir(box / "src")


def test_main_runs_the_baseline_before_scoring_any_mutant(run, repo, tmp_path, monkeypatch):
    """``check_baseline`` is worth nothing unless ``main()`` calls it first.

    The tests above drive the function directly, so deleting the one call
    in ``main()`` would survive them.  This goes through ``main()`` with
    a stub pytest that is always red and one mutation on file: the run
    must abort with the baseline message, and the stub must have been
    invoked exactly once -- the baseline -- with no mutant ever scored.
    Without the call, ``main()`` scores the mutant as KILLED off the
    pre-existing failure and returns normally.
    """
    calls = tmp_path / "calls"
    fake = _stub_pytest(
        tmp_path,
        f"echo x >> {calls}; echo 'FAILED tests/test_x.py::test_a - boom'; exit 1",
    )
    mutations = tmp_path / "mutations.json"
    mutations.write_text(json.dumps([{
        "id": "test_x.py:1:cmp:pass->assert False",
        "file": "tests/test_x.py",
        "old": "pass",
        "new": "assert False",
        "kind": "cmp",
    }]))
    out = tmp_path / "results.txt"
    monkeypatch.setattr(sys, "argv", [
        "run.py", str(mutations), str(out),
        "--repo", str(repo), "--pytest", fake,
        "--modules", "test_x.py", "--timeout", "30",
    ])
    with pytest.raises(SystemExit) as exc:
        run.main()
    assert "baseline" in str(exc.value).lower()
    assert calls.read_text().count("x") == 1, "stub pytest must run once: the baseline"
    assert "KILLED" not in out.read_text(), "no mutant may be scored after a red baseline"
