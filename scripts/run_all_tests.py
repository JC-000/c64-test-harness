#!/usr/bin/env python3
"""Run all c64-test-harness tests with parallel execution.

Discovers and runs test files in three phases:
  1. Unit tests — parallel, no external dependencies
  2. Integration tests — parallel, needs c1541
  3. VICE integration tests — serial, needs x64sc + c1541

Usage:
    python3 scripts/run_all_tests.py
    python3 scripts/run_all_tests.py --unit-only
    python3 scripts/run_all_tests.py --serial --verbose
    python3 scripts/run_all_tests.py --workers 4 -k "test_config"
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
)
TESTS_DIR = os.path.join(PROJECT_ROOT, "tests")


# ---------------------------------------------------------------------------
# Test registry
# ---------------------------------------------------------------------------

@dataclass
class TestSuite:
    """Declaration of a single test file."""

    file: str
    category: str  # "unit", "integration", "integration-vice"
    required_tools: list[str] = field(default_factory=list)
    serial: bool = False


# fmt: off
SUITES: list[TestSuite] = [
    # ── Unit tests (no external tools) ────────────────────────────
    TestSuite("test_config.py",        "unit"),
    TestSuite("test_keyboard.py",      "unit"),
    TestSuite("test_labels.py",        "unit"),
    TestSuite("test_memory_helpers.py", "unit"),
    TestSuite("test_memory_parse.py",  "unit"),
    TestSuite("test_parallel.py",      "unit"),
    TestSuite("test_petscii.py",       "unit"),
    TestSuite("test_port_allocator.py","unit"),
    TestSuite("test_runner.py",        "unit"),
    TestSuite("test_screen.py",        "unit"),
    TestSuite("test_screen_codes.py",  "unit"),
    TestSuite("test_verify.py",        "unit"),
    TestSuite("test_vice_manager.py",  "unit"),

    # ── Integration (needs c1541) ─────────────────────────────────
    TestSuite("test_disk.py",          "integration", required_tools=["c1541"]),

    # ── VICE integration (needs x64sc + c1541, uses fixed port) ───
    TestSuite("test_disk_vice.py",     "integration-vice",
              required_tools=["x64sc", "c1541"], serial=True),
]
# fmt: on


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class FileResult:
    """Outcome of running one test file."""

    suite: TestSuite
    returncode: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration: float = 0.0
    stdout: str = ""
    stderr: str = ""
    tool_skip: bool = False  # True when skipped due to missing tool
    skip_reason: str = ""

    @property
    def status(self) -> str:
        if self.tool_skip:
            return "SKIP"
        if self.returncode == 0:
            return "PASS"
        return "FAIL"

    @property
    def total_collected(self) -> int:
        return self.passed + self.failed + self.errors + self.skipped


# Regex for pytest summary line, e.g. "3 passed, 1 failed, 2 skipped"
_SUMMARY_RE = re.compile(
    r"(?:=+\s*)?"
    r"((?:\d+\s+\w+(?:,\s*)?)+)"
    r"(?:\s+in\s+[\d.]+s)?"
    r"(?:\s*=+)?$",
    re.MULTILINE,
)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|skipped|warnings?|deselected)")


def _parse_pytest_summary(stdout: str) -> dict[str, int]:
    """Extract counts from the last pytest summary line."""
    counts: dict[str, int] = {}
    for m in _SUMMARY_RE.finditer(stdout):
        segment = m.group(1)
        for cm in _COUNT_RE.finditer(segment):
            key = cm.group(2).rstrip("s")  # normalise "warnings" -> "warning"
            counts[key] = int(cm.group(1))
    return counts


# ---------------------------------------------------------------------------
# Tool detection
# ---------------------------------------------------------------------------

def _check_tools(required: list[str]) -> str | None:
    """Return the name of the first missing tool, or None if all present."""
    for tool in required:
        if shutil.which(tool) is None:
            return tool
    return None


# ---------------------------------------------------------------------------
# Single-file runner
# ---------------------------------------------------------------------------

def _run_one(
    suite: TestSuite,
    *,
    verbose: bool = False,
    k_pattern: str | None = None,
) -> FileResult:
    """Run a single test file via pytest subprocess."""
    # Check tool availability first
    if suite.required_tools:
        missing = _check_tools(suite.required_tools)
        if missing is not None:
            return FileResult(
                suite=suite,
                tool_skip=True,
                skip_reason=f"{missing} not found",
            )

    path = os.path.join(TESTS_DIR, suite.file)
    cmd = [sys.executable, "-m", "pytest", path, "-v", "--tb=short"]
    if k_pattern:
        cmd.extend(["-k", k_pattern])

    t0 = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=PROJECT_ROOT)
    duration = time.monotonic() - t0

    counts = _parse_pytest_summary(proc.stdout)

    result = FileResult(
        suite=suite,
        returncode=proc.returncode,
        passed=counts.get("passed", 0),
        failed=counts.get("failed", 0),
        errors=counts.get("error", 0),
        skipped=counts.get("skipped", 0),
        duration=duration,
        stdout=proc.stdout,
        stderr=proc.stderr,
    )

    if verbose:
        print(proc.stdout)
        if proc.stderr:
            print(proc.stderr, file=sys.stderr)

    return result


# ---------------------------------------------------------------------------
# Phase runner
# ---------------------------------------------------------------------------

def _run_phase(
    label: str,
    suites: list[TestSuite],
    *,
    max_workers: int,
    verbose: bool = False,
    k_pattern: str | None = None,
) -> list[FileResult]:
    """Run a list of test suites and return results."""
    if not suites:
        return []

    serial = max_workers <= 1 or all(s.serial for s in suites)
    mode = "serial" if serial else f"parallel, {max_workers} workers"
    print(f"\n  {label} ({len(suites)} file{'s' if len(suites) != 1 else ''}, {mode})")

    results: list[FileResult] = []

    if serial:
        for suite in suites:
            r = _run_one(suite, verbose=verbose, k_pattern=k_pattern)
            _print_file_result(r)
            results.append(r)
    else:
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_one, s, verbose=verbose, k_pattern=k_pattern): s
                for s in suites
            }
            for fut in as_completed(futures):
                r = fut.result()
                _print_file_result(r)
                results.append(r)

    return results


def _print_file_result(r: FileResult) -> None:
    """Print a single result line."""
    name = r.suite.file
    if r.tool_skip:
        print(f"    {name:<30s} SKIP   ---    {r.skip_reason}")
    else:
        counts = f"{r.passed} passed"
        if r.failed:
            counts += f", {r.failed} failed"
        if r.errors:
            counts += f", {r.errors} errors"
        if r.skipped:
            counts += f", {r.skipped} skipped"
        print(f"    {name:<30s} {r.status:<4s}   {r.duration:5.1f}s  {counts}")


# ---------------------------------------------------------------------------
# Unregistered file warning
# ---------------------------------------------------------------------------

def _warn_unregistered() -> None:
    """Warn about test_*.py files not in the registry."""
    registered = {s.file for s in SUITES}
    for entry in sorted(os.listdir(TESTS_DIR)):
        if entry.startswith("test_") and entry.endswith(".py") and entry not in registered:
            print(f"  WARNING: {entry} is not registered in SUITES — it will not be run")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Run all c64-test-harness tests with parallel execution.",
    )
    p.add_argument(
        "--unit-only",
        action="store_true",
        help="Skip integration tests (run only unit tests)",
    )
    p.add_argument(
        "--serial",
        action="store_true",
        help="Disable parallelism — run every file sequentially",
    )
    p.add_argument(
        "--workers",
        type=int,
        default=0,
        metavar="N",
        help="Max parallel workers (default: min(file_count, cpu_count))",
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help="Show full pytest output for each file",
    )
    p.add_argument(
        "-k",
        dest="k_pattern",
        default=None,
        metavar="PATTERN",
        help="Pass-through to pytest -k for test selection",
    )
    return p


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    args = _build_parser().parse_args()

    # Categorise suites
    unit_suites = [s for s in SUITES if s.category == "unit"]
    integration_suites = [s for s in SUITES if s.category == "integration"]
    vice_suites = [s for s in SUITES if s.category == "integration-vice"]

    # Decide worker count
    if args.serial:
        workers = 1
    elif args.workers > 0:
        workers = args.workers
    else:
        cpu = os.cpu_count() or 4
        workers = min(len(unit_suites), cpu)

    wall_start = time.monotonic()

    print("=" * 64)
    print("  TEST RESULTS SUMMARY")
    print("=" * 64)

    _warn_unregistered()

    all_results: list[FileResult] = []

    # Phase 1: Unit tests
    all_results.extend(
        _run_phase(
            "PHASE 1: Unit Tests",
            unit_suites,
            max_workers=workers,
            verbose=args.verbose,
            k_pattern=args.k_pattern,
        )
    )

    # Phase 2: Integration tests (c1541)
    if not args.unit_only:
        all_results.extend(
            _run_phase(
                "PHASE 2: Integration",
                integration_suites,
                max_workers=workers,
                verbose=args.verbose,
                k_pattern=args.k_pattern,
            )
        )

        # Phase 3: VICE integration tests (serial)
        all_results.extend(
            _run_phase(
                "PHASE 3: VICE Integration",
                vice_suites,
                max_workers=1,
                verbose=args.verbose,
                k_pattern=args.k_pattern,
            )
        )

    wall_time = time.monotonic() - wall_start

    # ── Final summary ─────────────────────────────────────────────
    print()
    print("-" * 64)

    total_run = sum(1 for r in all_results if not r.tool_skip)
    total_skip = sum(1 for r in all_results if r.tool_skip)
    total_passed = sum(r.passed for r in all_results)
    total_failed = sum(r.failed for r in all_results)
    total_errors = sum(r.errors for r in all_results)

    parts = [f"{total_passed} passed"]
    if total_failed:
        parts.append(f"{total_failed} failed")
    if total_errors:
        parts.append(f"{total_errors} errors")

    print(
        f"  Total: {total_run} run, {total_skip} skipped"
        f" | {' | '.join(parts)}"
        f" | {wall_time:.1f}s wall time"
    )
    print("=" * 64)

    if total_failed or total_errors:
        # Show failures with their output for quick debugging
        print("\n  FAILURES:")
        for r in all_results:
            if r.failed or r.errors:
                print(f"\n  --- {r.suite.file} ---")
                print(r.stdout)
        return 1

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(2)
    except Exception as exc:
        print(f"\nRunner error: {exc}", file=sys.stderr)
        sys.exit(2)
