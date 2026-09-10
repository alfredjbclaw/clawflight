#!/usr/bin/env bash
# Apply the PII blocklist to every blob reachable from any ref.
#
# tests/test_no_pii.py gates the working tree and (when git is available) git
# history on every test run. This script is the standalone form to run before
# publishing, and again after any history rewrite.
#
# Usage:
#   tests/history_scan.sh              scan all git history
#   tests/history_scan.sh --worktree   scan the working tree only
#   tests/history_scan.sh --strict     also flag the owner's surname
#
# Exit status 0 means clean.
set -euo pipefail

cd "$(dirname "$0")/.."

# Third-party personal data. None of this may exist anywhere, ever.
PATTERNS=(
  'berchtold\.org'
  '\b(ledecompany|livenation)\b'
  '@(cornell\.edu|gmail\.com|icloud\.com|outlook\.com|yahoo\.com)\b'
  '(skymiles|aadvantage|mileageplus)[^\n]{0,24}[0-9]{6,}'
  '(e-?ticket|ticket (number|#))[^\n]{0,24}[0-9]{10,}'
  '(amex|american express|visa|mastercard)[^\n]{0,24}(\*{2,}|x{4,}|ending in )[0-9]{4}'
  '(\+1[ .-]?)?\(?[2-9][0-9]{2}\)?[ .-][2-9][0-9]{2}[ .-][0-9]{4}'
)

worktree_only=0
for argument in "$@"; do
  case "${argument}" in
    --worktree) worktree_only=1 ;;
    --strict)   PATTERNS+=('berchtold') ;;
    *) echo "unknown option: ${argument}" >&2; exit 2 ;;
  esac
done

# The gate and this script necessarily contain every pattern they forbid.
EXCLUDES=(':!tests/test_no_pii.py' ':!tests/history_scan.sh')

if [[ ${worktree_only} -eq 1 ]]; then
  label='working tree'
  SEARCH=(--untracked)
else
  label='all git history'
  # shellcheck disable=SC2207
  SEARCH=($(git rev-list --all))
  if [[ ${#SEARCH[@]} -eq 0 ]]; then
    echo 'no commits to scan'
    exit 0
  fi
fi

echo "scanning ${label} for ${#PATTERNS[@]} blocked patterns"
status=0

for pattern in "${PATTERNS[@]}"; do
  if hits=$(git grep -I -i -n -E "${pattern}" "${SEARCH[@]}" -- "${EXCLUDES[@]}" 2>/dev/null); then
    echo
    echo "FAIL  ${pattern}"
    echo "${hits}" | head -40
    status=1
  fi
done

if [[ ${status} -eq 0 ]]; then
  echo "clean: no blocked pattern found in ${label}"
else
  cat <<'REMEDIATION'

Blocked patterns found.

If the hits are in the working tree, edit the files and re-run.

If the hits exist only in history, editing the working tree is NOT enough: the
blobs stay reachable. Rewrite history, then force-push and discard every
existing clone and fork.

  # single-commit history: amend the root commit in place
  git commit --amend --no-edit

  # longer history: rewrite every commit that touched the file
  git filter-repo --path <file> --invert-paths

Re-run this script after the rewrite.
REMEDIATION
fi

exit ${status}
