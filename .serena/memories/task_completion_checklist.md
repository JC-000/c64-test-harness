# When a task is complete

1. **Run relevant tests** with the venv interpreter:
   ```zsh
   ~/.local/share/c64-test-harness/venv/bin/pytest tests/<relevant file>.py -v
   ```
   For broader changes, run the whole suite. Expect tests that need Linux bridge networking to skip on macOS (5 files) and tests gated by `U64_HOST` to skip without hardware — both are fine.

2. **Run `./scripts/verify-dev-env.sh`** if you touched anything in `scripts/` or the install flow. Expect `Overall: READY (with optional gaps)` on macOS (the Linux bridge/iproute2/iptables/tun rows are non-critical and always show missing).

3. **No linter, no formatter, no type checker**. Don't add one. The test suite is the gate.

4. **Review `git diff`** before asking the user to commit — confirm the diff is surgical and doesn't include drive-by reformatting or unrelated changes.

5. **Never commit or push without explicit user request.** Leave changes in the working tree for review.

6. **Update docs when behavior changes**, specifically `README.md` and `docs/development.md`. New major features get a line in the README's feature bullet list; env-setup changes go in `docs/development.md`.

7. **If you added a new top-level public API**, re-export it from `src/c64_test_harness/__init__.py`.

8. **If you touched `scripts/*.sh`**, verify portability: `#!/usr/bin/env bash`, `set -u -o pipefail`, avoid `declare -A` (bash 3.2 on macOS), avoid `printf '\uXXXX'` (not portable to bash 3.2 — use `printf '%b' '\xxx'` octal).
