#!/usr/bin/env bash
# Thin shim over tests/pii_scan.py, kept because the docs and Makefile name it.
#
# The scanning logic is Python because it must share one pattern list with the
# pytest gate. An earlier shell implementation kept its own copy, and `git grep
# -E` implements POSIX ERE — which has no `\b` — so several patterns silently
# matched nothing and every run reported clean.
#
#   tests/history_scan.sh              all publishable history
#   tests/history_scan.sh --worktree   the working tree only
#   tests/history_scan.sh --strict     also flag the maintainer's own identity
#
# Exit status: 0 clean, 1 findings, 2 the scan could not run.
set -euo pipefail

exec python3 "$(dirname "$0")/pii_scan.py" "$@"
