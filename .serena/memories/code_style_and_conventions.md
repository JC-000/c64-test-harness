# Code style & conventions

No linter/formatter config in the repo. Observed conventions by reading the source:

- **Type hints**: used broadly on public APIs. Dataclasses for config objects (see `HarnessConfig` in `config.py`, `VICELifecycleConfig`-style dataclasses in backends).
- **Module style**: small focused modules (`screen.py`, `keyboard.py`, `memory.py` — one concern per file). No "utils.py" grab-bags.
- **Public API**: `src/c64_test_harness/__init__.py` re-exports the intended surface. New top-level exports go there.
- **Transport abstraction**: `C64Transport` Protocol in `transport.py` is the seam between VICE and Ultimate 64 backends. New features that need to work on both platforms go through the transport; VICE-only or U64-only features live in their respective backend modules.
- **Docstrings**: present on public functions, generally one short paragraph describing behavior + parameter semantics. Not in any formal schema (not Google/NumPy/Sphinx). Be concise.
- **Comments**: explain *why* something is unusual or non-obvious (constraints, upstream bugs, 6502 quirks). Avoid narrating *what* the code does.
- **Error handling**: raise specific exceptions with actionable messages; don't wrap-and-lose the original. At boundaries (VICE launch, TCP connect, disk images), provide actionable hints.
- **Tests**: `pytest` style, one class per behavior area. Fixtures in `tests/conftest.py` handle VICE launch/teardown + port allocation. Tests that need bridge networking skip with `pytest.skip("needs tap-c64-0/tap-c64-1")` rather than failing.
- **Scripts**: bash scripts under `scripts/` use `set -u -o pipefail` (not `-e`) so stages can continue. Every stage idempotent. Every mutating action gated behind `--dry-run`. Helper functions `log_ok` / `log_warn` / `log_fail` / `log_skip` used consistently.
- **macOS portability note**: scripts target Ubuntu Desktop 25 by default. Portability fixes are welcome but should be scoped (e.g. handle bash 3.2, use portable printf escapes, detect OS before Linux-specific commands).

## Things NOT to add
- No runtime dependencies (zero-dep is a design principle).
- No CLI entry points (library-only).
- No backwards-compatibility shims — change the code directly.
