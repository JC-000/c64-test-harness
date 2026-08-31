#!/usr/bin/env python3
"""Apply each mutation in an isolated copy and record which tests died.

A mutation nothing kills means the behaviour it changed is unguarded --
or that the mutant is equivalent, which the report has to distinguish by
hand.  See ``README.md``.

    python3 scripts/mutation/run.py mutations.json results.txt \\
        --modules test_vice_lifecycle.py test_vice_manager.py

Isolation is a full copy of ``src`` and ``tests`` per mutation, never an
edit in place: the working tree is often shared with other agents, and a
mutation left behind would be worse than no measurement.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import signal
import subprocess
import tempfile

DEFAULT_MODULES = [
    "test_vice_lifecycle.py",
    "test_vice_config.py",
    "test_vice_manager.py",
    "test_vice_binary_unit.py",
    "test_vice_binary_resource.py",
    "test_vice_elevation.py",
]


def run_suite(box: pathlib.Path, pytest_bin: str, modules: list[str],
              timeout: int) -> tuple[list[str], bool]:
    """Run *modules* inside *box*; return (failed test names, timed_out)."""
    env = dict(os.environ, PYTHONPATH=str(box / "src"))
    # A mutation run must never be gated by the live-emulator demand.
    env.pop("C64_REQUIRE_VICE", None)
    proc = subprocess.Popen(
        [pytest_bin, "-q", "--no-header", "-p", "no:cacheprovider", *modules],
        cwd=box / "tests", stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # One comparison mutation sends a wait loop unbounded and the
        # process into multi-GB territory, so this is a real outcome and
        # not a flake.  Kill the group: pytest may have children.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return [], True
    names = set()
    for line in out.splitlines():
        if line.startswith(("FAILED", "ERROR")):
            names.add(line.split("::")[1].split()[0].split("[")[0]
                      if "::" in line else line.strip()[:60])
    return sorted(names), False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mutations")
    ap.add_argument("out")
    ap.add_argument("--repo", default=str(pathlib.Path(__file__).resolve().parents[2]))
    ap.add_argument("--modules", nargs="*", default=DEFAULT_MODULES)
    ap.add_argument("--pytest", default=os.path.expanduser(
        "~/.local/share/c64-test-harness/venv/bin/pytest"))
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--start", type=int, default=0, help="resume at this index")
    args = ap.parse_args()

    repo = pathlib.Path(args.repo)
    muts = json.load(open(args.mutations))[args.start:]
    survivors: list[dict] = []
    with open(args.out, "w", buffering=1) as out, \
            tempfile.TemporaryDirectory(prefix="mutation-") as tmp:
        box = pathlib.Path(tmp) / "box"
        for i, m in enumerate(muts, 1 + args.start):
            shutil.rmtree(box, ignore_errors=True)
            box.mkdir(parents=True)
            for d in ("src", "tests"):
                shutil.copytree(repo / d, box / d)
            target = box / m["file"]
            src = target.read_text()
            if m["old"] not in src:
                out.write(f"NOTAPPLIED {m['id']}\n")
                continue
            # First-occurrence replacement -- see README, "known limits".
            target.write_text(src.replace(m["old"], m["new"], 1))
            failed, timed_out = run_suite(box, args.pytest, args.modules,
                                          args.timeout)
            if timed_out:
                status = "KILLED"
                detail = "<timeout/runaway>"
            elif failed:
                status = "KILLED"
                detail = str(failed[:3])
            else:
                status = "SURVIVED"
                detail = ""
                survivors.append(m)
            out.write(f"{status:9} [{i}/{len(muts) + args.start}] {m['id']} "
                      f"{detail}\n")
        out.write("\n===== SURVIVORS =====\n")
        for m in survivors:
            out.write(f"  {m['id']}\n     {m['file']}: "
                      f"{m['old']!r} -> {m['new']!r}\n")
        out.write(f"\ntotal={len(muts)} survivors={len(survivors)}\n")
    print(f"survivors={len(survivors)} of {len(muts)}")


if __name__ == "__main__":
    main()
