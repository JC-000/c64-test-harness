#!/usr/bin/env python3
"""Generate a mutation population mechanically from the VICE backend source.

Mechanical on purpose: a hand-picked mutation list finds the weaknesses
its author already suspected.  See ``README.md`` for the population
definition and its known limitations.

    python3 scripts/mutation/generate.py mutations.json [--repo PATH]
"""
from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re

#: The four modules that make up the VICE backend.  Mutating anything
#: else would be measuring a different suite.
TARGETS = [
    "src/c64_test_harness/backends/vice_lifecycle.py",
    "src/c64_test_harness/backends/vice_binary.py",
    "src/c64_test_harness/backends/vice_elevation.py",
    "src/c64_test_harness/backends/vice_manager.py",
]

#: Flags belonging to *other* programs these modules invoke.  Mutating
#: them produces a permanent survivor — the argv contract checks emitted
#: flags against ``x64sc -help``, and `ps` argv is not in it — so they are
#: noise of the same kind as a NOTAPPLIED row: a result that means "this
#: tool is measuring something it should not be".  Excluded with the
#: owning program named, so the filter is auditable rather than silent.
SIBLING_TOOL_FLAGS = {
    "-axo": "ps, in ViceProcess's sudo-child resolver",
}

#: A CLI flag: a leading -/+ then lowercase alphanumerics, which may
#: contain internal hyphens.
#:
#: The hyphen matters.  An earlier version was ``[-+][a-z0-9]{3,}``, which
#: silently skipped every hyphenated flag -- and ``-autostart-warp`` /
#: ``+autostart-warp`` are emitted unconditionally (vice_lifecycle.py:514),
#: so two real flags were never mutated in any sweep.  Found by diffing
#: this population against an independently-extracted one; a count that
#: matched (37 literals both ways) hid the fact that the two sets were
#: not the same 37.
FLAG_RE = re.compile(r"[-+][a-z0-9][a-z0-9-]{2,}")
#: A VICE resource name: CamelCase, five characters or more.
RESOURCE_RE = re.compile(r"[A-Z][A-Za-z0-9]{4,}")


def generate(repo: pathlib.Path) -> list[dict]:
    muts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    sources: dict[str, str] = {}

    def add(rel: str, old: str, new: str, kind: str, line: int) -> None:
        key = (rel, old, new)
        if key in seen or old == new:
            return
        # Never emit a mutation whose pattern is not in the source.  The
        # runner would report it NOTAPPLIED -- a row that means "this tool
        # did nothing" and that a reader skips over.  Tolerating those is
        # how `-autostart-warp` stayed unmutated for a week: the one
        # output that would have prompted a look at the extraction was
        # being routinely ignored.  A no-op result from your own tooling
        # is a defect to remove, not a row to skip.
        if old not in sources[rel]:
            return
        seen.add(key)
        muts.append(
            {
                "id": f"{rel.split('/')[-1]}:{line}:{kind}:{old[:34]}->{new[:24]}",
                "file": rel,
                "old": old,
                "new": new,
                "kind": kind,
            }
        )

    for rel in TARGETS:
        src = (repo / rel).read_text()
        sources[rel] = src
        tree = ast.parse(src)
        lines = src.splitlines()
        fragments = {
            id(c)
            for j in ast.walk(tree)
            if isinstance(j, ast.JoinedStr)
            for c in ast.walk(j)
            if isinstance(c, ast.Constant)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in fragments):
                v = node.value
                if v in SIBLING_TOOL_FLAGS:
                    continue
                if FLAG_RE.fullmatch(v):
                    add(rel, f'"{v}"', f'"{v}X"', "flagname", node.lineno)
                    flip = ("+" if v[0] == "-" else "-") + v[1:]
                    add(rel, f'"{v}"', f'"{flip}"', "flagpolarity", node.lineno)
                elif RESOURCE_RE.fullmatch(v):
                    add(rel, f'"{v}"', f'"{v}X"', "resname", node.lineno)
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, int)
                and not isinstance(node.value, bool)
                and 0 <= node.value <= 8
            ):
                ln = lines[node.lineno - 1]
                if "=" in ln or "," in ln:
                    add(rel, f"= {node.value}", f"= {node.value + 1}", "int",
                        node.lineno)
            if isinstance(node, ast.Compare) and len(node.ops) == 1:
                seg = ast.get_source_segment(src, node)
                if seg and len(seg) < 60:
                    for a, b in (("<=", "<"), (">=", ">"), ("==", "!="),
                                 ("is not None", "is None")):
                        if a in seg:
                            add(rel, seg, seg.replace(a, b, 1), "cmp", node.lineno)
                            break
    return muts


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--repo", default=str(pathlib.Path(__file__).resolve().parents[2]))
    args = ap.parse_args()
    muts = generate(pathlib.Path(args.repo))
    pathlib.Path(args.out).write_text(json.dumps(muts, indent=1))
    by_kind: dict[str, int] = {}
    for m in muts:
        by_kind[m["kind"]] = by_kind.get(m["kind"], 0) + 1
    print(f"{len(muts)} mutations: " +
          ", ".join(f"{k}={v}" for k, v in sorted(by_kind.items())))


if __name__ == "__main__":
    main()
