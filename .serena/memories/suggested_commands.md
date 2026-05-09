# Suggested commands (macOS / Darwin development)

Host: macOS 26.4 (Tahoe) / arm64. Shell: zsh. The system is Darwin — standard BSD utilities, not GNU. Homebrew at `/opt/homebrew`.

## Venv (always use this, not system python3)
```zsh
source ~/.local/share/c64-test-harness/venv/bin/activate
# or invoke directly without activating:
~/.local/share/c64-test-harness/venv/bin/python
~/.local/share/c64-test-harness/venv/bin/pytest
```
System `python3` is 3.9 (too old; harness needs >=3.10). Always go through the venv interpreter.

## Testing
```zsh
# Full suite
~/.local/share/c64-test-harness/venv/bin/pytest

# Single file
~/.local/share/c64-test-harness/venv/bin/pytest tests/test_vice_core.py -v

# Single test
~/.local/share/c64-test-harness/venv/bin/pytest tests/test_vice_core.py::TestName::test_method -v

# Stop on first failure, verbose
~/.local/share/c64-test-harness/venv/bin/pytest -x -v
```
Tests requiring Linux bridge networking skip cleanly on macOS (5 of ~65 files). Tests gated by `U64_HOST` env var skip when unset.

## Env verification
```zsh
./scripts/verify-dev-env.sh            # read-only diagnostic; exit 0=READY
./scripts/verify-dev-env.sh --json     # machine-readable
```

## VICE sanity probes on macOS
`x64sc --version` is **broken** on macOS 26 (Homebrew bottle argv[0] bug — exits 1 with "argv[0] is NULL, giving up"). Use these instead:
```zsh
x64sc -features                        # prints compile-time flags; confirms HAVE_RAWNET / HAVE_PCAP
x64sc -help 2>&1 | grep ethernet       # ethernet cart flags are present when ethernet is compiled in
brew list --versions vice              # -> "vice 3.10"
```

## Typical Darwin / BSD gotchas vs Linux
- `sed -i ''` (BSD) vs `sed -i` (GNU). Prefer Python one-liners for in-place edits.
- `find` on Darwin does NOT support `-printf`.
- No `/proc/self/exe`, no `/dev/net/tun`, no `ip`/`iptables`/`bridge` (iproute2). `ifconfig` is the BSD tool.
- Default `bash` is 3.2 (no associative arrays, no `${var,,}`, no `\u` printf escapes); use `/bin/zsh` or Homebrew bash for 4+ features.
- `multiprocessing` default start method is `spawn` (not `fork` like Linux); any test or helper script passing a nested closure as `Process(target=...)` blows up with `AttributeError: Can't get local object`. Hoist workers to module scope and pass closure variables via `args=`. Already applied to `scripts/stress_cross_process.py`; `tests/test_port_lock.py` still has nested helpers and fails on macOS.

## Git
```zsh
git status
git diff
git log --oneline -20
git fetch origin
# Remote is HTTPS (not SSH); gh auth covers it.
```

## Branch work
Develop on topic branches off `master`. Do NOT push/merge without the user asking.
