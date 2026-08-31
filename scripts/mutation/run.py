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
              timeout: int) -> tuple[str, str]:
    """Run *modules* inside *box*; return (verdict, detail).

    Verdict is one of KILLED, SURVIVED or NOTVIABLE.

    **The return code is authoritative, not the output text.** An earlier
    version scored purely by scanning for ``FAILED``/``ERROR`` lines, and
    a mutation that broke the parse produced neither: pytest reports a
    conftest ``ImportError`` with no such prefix, so the mutant scored as
    a *survivor*. That inflates the survivor count and therefore inflates
    how much any later guard appears to kill. Measured: a seeded syntax
    error and a seeded bad import both scored SURVIVED under the old
    scorer.

    NOTVIABLE is a third verdict on purpose. A mutant that stops the
    suite collecting tells you nothing about test quality, so counting it
    as killed would flatter the kill rate exactly as counting it as
    survived flatters the survivor count. It belongs in neither, and is
    excluded from both.
    """
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
        # process into multi-GB territory.  A hang is a behavioural
        # difference the suite exposed, so it counts as a kill.
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        proc.communicate()
        return "KILLED", "<timeout/runaway>"

    rc = proc.returncode
    names = set()
    for line in out.splitlines():
        if line.startswith(("FAILED", "ERROR")):
            names.add(line.split("::")[1].split()[0].split("[")[0]
                      if "::" in line else line.strip()[:60])

    # pytest: 0 all passed, 1 tests failed, 2 interrupted, 3 internal
    # error, 4 usage error (this is where a broken import lands), 5 no
    # tests collected.
    if rc in (2, 3, 4, 5):
        first = next((l for l in out.splitlines() if l.strip()), "")
        return "NOTVIABLE", f"rc={rc} {first[:70]}"
    if rc == 0 and not names:
        return "SURVIVED", ""
    if names:
        return "KILLED", str(sorted(names)[:3])
    return "NOTVIABLE", f"rc={rc} with no reported failures"


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
    notviable: list[dict] = []
    notapplied: list[dict] = []
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
                # The pattern is not in the source at all (e.g. a literal
                # that only exists as an f-string fragment).  Not a mutant.
                notapplied.append(m)
                out.write(f"NOTAPPLIED {m['id']}\n")
                continue
            # First-occurrence replacement -- see README, "known limits".
            target.write_text(src.replace(m["old"], m["new"], 1))
            verdict, detail = run_suite(box, args.pytest, args.modules,
                                        args.timeout)
            if verdict == "SURVIVED":
                survivors.append(m)
            elif verdict == "NOTVIABLE":
                notviable.append(m)
            out.write(f"{verdict:9} [{i}/{len(muts) + args.start}] {m['id']} "
                      f"{detail}\n")
        out.write("\n===== SURVIVORS =====\n")
        for m in survivors:
            out.write(f"  {m['id']}\n     {m['file']}: "
                      f"{m['old']!r} -> {m['new']!r}\n")
        out.write("\n===== NOT VIABLE (excluded from both counts) =====\n")
        for m in notviable:
            out.write(f"  {m['id']}\n")
        # Viable excludes BOTH exclusions: a mutant that never applied and
        # one that broke collection are equally uninformative about test
        # quality, and counting either as killed flatters the kill rate.
        viable = len(muts) - len(notviable) - len(notapplied)
        killed = viable - len(survivors)
        out.write(
            f"\npopulation={len(muts)} notapplied={len(notapplied)} "
            f"notviable={len(notviable)} viable={viable} "
            f"survivors={len(survivors)} killed={killed}\n"
        )
    print(f"population={len(muts)} notapplied={len(notapplied)} "
          f"notviable={len(notviable)} viable={viable} "
          f"survivors={len(survivors)} killed={killed}")


if __name__ == "__main__":
    main()
