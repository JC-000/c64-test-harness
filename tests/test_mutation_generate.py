"""``scripts/mutation/generate.py`` must not emit mutations that land in prose.

``run.py`` applies a mutation by replacing the *first* textual occurrence
of its pattern.  When that first occurrence is inside a docstring or a
comment, the mutant changes no code, the suite passes, and the row is
scored as a survivor -- a no-op result dressed as a coverage hole.  Three
such rows were in the population at 9e5d7fa (``vice_elevation.py``: the
``"-features"`` name and polarity mutants, whose first occurrence is the
docstring at :155, and an ``int`` mutant whose ``= 0`` first matches the
module docstring's ``geteuid() == 0`` at :6).

The generator already refuses to emit a pattern that is absent from the
source, on the principle that a no-op result from your own tooling is a
defect to remove, not a row to skip.  A pattern whose first occurrence is
documentation is the same defect, and these tests hold it to the same
rule.
"""

from __future__ import annotations

import importlib.util
import pathlib
import textwrap

import pytest

_GENERATE = (
    pathlib.Path(__file__).resolve().parents[1] / "scripts" / "mutation" / "generate.py"
)


@pytest.fixture(scope="module")
def generate():
    spec = importlib.util.spec_from_file_location("mutation_generate", _GENERATE)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


REL = "src/pkg/mod.py"


def _repo(tmp_path: pathlib.Path, source: str) -> pathlib.Path:
    target = tmp_path / REL
    target.parent.mkdir(parents=True)
    target.write_text(textwrap.dedent(source))
    return tmp_path


def _olds(muts: list[dict]) -> set[str]:
    return {m["old"] for m in muts}


def test_a_code_literal_with_no_prose_twin_is_mutated(generate, tmp_path):
    """Positive control: the exclusion must not eat genuine mutations."""
    repo = _repo(
        tmp_path,
        '''
        def start():
            args = ["-default", "-console"]
            retries = 3
            return args
        ''',
    )
    olds = _olds(generate.generate(repo, targets=[REL]))
    assert '"-default"' in olds
    assert '"-console"' in olds
    assert "= 3" in olds


def test_a_literal_whose_first_occurrence_is_a_docstring_is_skipped(generate, tmp_path):
    """The ``"-features"`` case from vice_elevation.py, reduced.

    The literal *is* in code, but ``run.py`` would replace the docstring
    copy first, so the mutant is a no-op.  The int case rides along: a
    docstring saying ``timeout == 0`` contains ``= 0`` before the code's
    ``retries = 0`` does.
    """
    repo = _repo(
        tmp_path,
        '''
        """Runs x64sc "-features" and treats timeout == 0 as no timeout."""


        def probe():
            args = ["-features", "-default"]
            retries = 0
            return args, retries
        ''',
    )
    olds = _olds(generate.generate(repo, targets=[REL]))
    assert '"-features"' not in olds, "first occurrence is the module docstring"
    assert "= 0" not in olds, "first '= 0' is the docstring's '== 0'"
    # The exclusion is per-pattern, not per-module.
    assert '"-default"' in olds


def test_a_literal_whose_first_occurrence_is_a_comment_is_skipped(generate, tmp_path):
    repo = _repo(
        tmp_path,
        '''
        def start():
            # VICE wants "-console" before every other flag
            args = ["-console", "-default"]
            return args
        ''',
    )
    olds = _olds(generate.generate(repo, targets=[REL]))
    assert '"-console"' not in olds, "first occurrence is a comment"
    assert '"-default"' in olds


def test_skipped_prose_hits_are_reported_not_silent(generate, tmp_path):
    """An exclusion the tool does not announce is a row nobody can audit."""
    repo = _repo(
        tmp_path,
        '''
        """Mentions "-features" first."""
        args = ["-features"]
        ''',
    )
    excluded: list[dict] = []
    muts = generate.generate(repo, targets=[REL], excluded=excluded)
    assert '"-features"' not in _olds(muts)
    assert any(m["old"] == '"-features"' for m in excluded), excluded


def test_the_real_targets_carry_no_prose_first_hits(generate):
    """Against the actual backend modules: nothing skipped is emitted.

    Recomputes the exclusion independently of the generator -- a
    docstring/comment span check over each emitted pattern's first
    occurrence -- so the generator's own filter is not its own oracle.
    """
    import ast
    import bisect
    import io
    import tokenize

    repo = _GENERATE.parents[2]
    muts = generate.generate(repo)
    assert muts, "generator produced nothing against the real backend"
    for rel in generate.TARGETS:
        src = (repo / rel).read_text()
        tree = ast.parse(src)
        offsets = [0]
        for line in src.splitlines(keepends=True):
            offsets.append(offsets[-1] + len(line))

        def pos(lineno: int, col: int) -> int:
            return offsets[lineno - 1] + col

        spans: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                body = node.body
                if (
                    body
                    and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)
                ):
                    d = body[0]
                    spans.append((pos(d.lineno, d.col_offset), pos(d.end_lineno, d.end_col_offset)))
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                spans.append((pos(*tok.start), pos(*tok.end)))
        starts = sorted(spans)
        for m in muts:
            if m["file"] != rel:
                continue
            first = src.find(m["old"])
            assert first >= 0, m["id"]
            idx = bisect.bisect_right(starts, (first, float("inf"))) - 1
            in_prose = idx >= 0 and starts[idx][0] <= first < starts[idx][1]
            assert not in_prose, (
                f"{m['id']}: first occurrence of {m['old']!r} is documentation; "
                f"run.py would mutate prose and score a survivor"
            )
