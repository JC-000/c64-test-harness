#!/usr/bin/env bash
# install-skill.sh — make the c64-test Claude Code skill available in every
# Claude Code session on this machine, regardless of CWD.
#
# Mechanism: symlink ``~/.claude/skills/c64-test`` to the skill directory in
# this repo (``<repo>/.claude/skills/c64-test``). Single source of truth —
# any skill update committed to the repo is visible to every Claude Code
# session the moment you pull the branch that contains it.
#
# Usage:
#   ./scripts/install-skill.sh                  # install (idempotent)
#   ./scripts/install-skill.sh --uninstall      # remove the symlink
#   ./scripts/install-skill.sh --dry-run        # show what would happen
#
# Exit 0 on success or already-installed. Exit 1 on conflict (a
# non-matching file/dir already occupies the target).

set -euo pipefail

SKILL_NAME="c64-test"
SKILLS_DIR="$HOME/.claude/skills"
TARGET="$SKILLS_DIR/$SKILL_NAME"

# Resolve this script's directory so we can locate the repo source dir even
# when the script is invoked via an absolute path or via $PATH.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
SOURCE="$REPO_ROOT/.claude/skills/$SKILL_NAME"

MODE="install"
DRY_RUN=0

for arg in "$@"; do
    case "$arg" in
        --uninstall)
            MODE="uninstall" ;;
        --dry-run)
            DRY_RUN=1 ;;
        -h|--help)
            sed -n '2,17p' "$0"
            exit 0 ;;
        *)
            echo "unknown arg: $arg" >&2
            exit 2 ;;
    esac
done

run() {
    # Execute *or* print the command based on DRY_RUN.
    if [[ "$DRY_RUN" == "1" ]]; then
        echo "DRY-RUN: $*"
    else
        eval "$@"
    fi
}

echo "Skill:  $SKILL_NAME"
echo "Source: $SOURCE"
echo "Target: $TARGET"
echo

if [[ ! -d "$SOURCE" ]]; then
    echo "[fail] source skill dir missing: $SOURCE" >&2
    echo "       make sure this script is run from inside the c64-test-harness repo," >&2
    echo "       and that .claude/skills/$SKILL_NAME/ is present." >&2
    exit 1
fi

case "$MODE" in
    install)
        if [[ -L "$TARGET" ]]; then
            # Symlink exists; check it points at us.
            current="$(readlink "$TARGET")"
            if [[ "$current" == "$SOURCE" ]]; then
                echo "[ok] already installed ($TARGET -> $SOURCE)"
                exit 0
            fi
            echo "[fail] $TARGET is a symlink but points to a different source:" >&2
            echo "       current: $current" >&2
            echo "       wanted:  $SOURCE" >&2
            echo "       Remove or update it manually, then re-run." >&2
            exit 1
        fi

        if [[ -e "$TARGET" ]]; then
            echo "[fail] $TARGET exists and is NOT a symlink." >&2
            echo "       Refusing to overwrite. Move or delete it, then re-run." >&2
            exit 1
        fi

        run "mkdir -p '$SKILLS_DIR'"
        run "ln -s '$SOURCE' '$TARGET'"
        echo "[ok] installed: $TARGET -> $SOURCE"
        echo
        echo "Claude Code will discover the skill on its next session start."
        echo "To uninstall: $0 --uninstall"
        ;;

    uninstall)
        if [[ ! -e "$TARGET" && ! -L "$TARGET" ]]; then
            echo "[ok] nothing to do: $TARGET does not exist"
            exit 0
        fi
        if [[ ! -L "$TARGET" ]]; then
            echo "[fail] $TARGET is not a symlink; refusing to delete a real file/dir." >&2
            exit 1
        fi
        current="$(readlink "$TARGET")"
        if [[ "$current" != "$SOURCE" ]]; then
            echo "[fail] $TARGET points at $current, not this repo ($SOURCE)." >&2
            echo "       Refusing to remove someone else's symlink." >&2
            exit 1
        fi
        run "rm '$TARGET'"
        echo "[ok] uninstalled: $TARGET"
        ;;
esac
