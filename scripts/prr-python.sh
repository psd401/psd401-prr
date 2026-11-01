#!/usr/bin/env bash
# ABOUTME: Prints the Python interpreter every gate lane uses. One copy.
# ABOUTME: A git worktree has no .venv of its own, so it borrows the main checkout's.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
common="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || :)"
main="${common%/.git}"

if [[ -n "${PRR_PYTHON_BIN:-}" ]]; then
  echo "$PRR_PYTHON_BIN"
elif [[ -x "$root/.venv/bin/python" ]]; then
  echo "$root/.venv/bin/python"
elif [[ -n "$common" && -x "$main/.venv/bin/python" ]]; then
  echo "$main/.venv/bin/python"
else
  echo python3
fi
