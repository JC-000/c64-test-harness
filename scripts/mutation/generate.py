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

#: A CLI flag: a leading -/+ then lowercase alphanumerics.
FLAG_RE = re.compile(r"[-+][a-z0-9]{3,}")
#: A VICE resource name: CamelCase, five characters or more.
RESOURCE_RE = re.compile(r"[A-Z][A-Za-z0-9]{4,}")


def generate(repo: pathlib.Path) -> list[dict]:
    muts: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    def add(rel: str, old: str, new: str, kind: str, line: int) -> None:
        key = (rel, old, new)
        if key in seen or old == new:
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
        tree = ast.parse(src)
        lines = src.splitlines()
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
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
