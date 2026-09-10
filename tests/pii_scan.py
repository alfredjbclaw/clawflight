#!/usr/bin/env python3
"""Scan everything this repository would publish for third-party personal data.

    tests/pii_scan.py              every blob and commit a push would expose
    tests/pii_scan.py --worktree   the working tree only
    tests/pii_scan.py --strict     also flag the maintainer's own identity

Exit status: 0 clean, 1 findings, 2 the scan could not run.

That last one matters. A scanner that fails must never look like a clean one,
so every git invocation is checked and a canary confirms the scan can actually
find something before any clean result is trusted.

History mode enumerates blobs by object id via ``git rev-list --objects``, so
each distinct blob is read once no matter how many commits contain it, and
commit author/committer identity is checked as well — that is published with
the code and no content scan can see it.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pii_blocklist import (  # noqa: E402
    SKIP_DIRS,
    SKIP_SUFFIXES,
    blocklist,
    findings_in,
    is_self_referential,
    without_maintainer,
)


REPO = Path(__file__).resolve().parent.parent


class ScanError(RuntimeError):
    """The scan could not be completed. Never report this as clean."""


def _git(*arguments: str, binary: bool = False):
    result = subprocess.run(
        ["git", *arguments],
        cwd=str(REPO),
        capture_output=True,
        timeout=300,
        check=False,
    )
    if result.returncode != 0:
        raise ScanError(
            "git {} failed ({}): {}".format(
                " ".join(arguments[:2]),
                result.returncode,
                result.stderr.decode("utf-8", "replace").strip()[:400],
            )
        )
    return result.stdout if binary else result.stdout.decode("utf-8", "replace")


# --------------------------------------------------------------------------
# Working tree
# --------------------------------------------------------------------------


def worktree_files() -> List[Path]:
    files = []
    for path in sorted(REPO.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if is_self_referential(str(relative)):
            continue
        files.append(path)
    return files


def scan_worktree(strict: bool) -> Tuple[List[str], int]:
    findings: List[str] = []
    scanned = 0
    for path in worktree_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        findings.extend(findings_in(text, str(path.relative_to(REPO)), strict))
    return findings, scanned


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def publishable_blobs() -> Dict[str, str]:
    """Object id -> path, for every blob a push would expose.

    ``--branches --tags`` on purpose: that is exactly what publishing exposes.
    Remote-tracking refs belong to the remote, and ``refs/original/*`` left by a
    history rewrite must be deleted rather than scanned — a rewrite is not
    finished until it is gone.
    """
    blobs: Dict[str, str] = {}
    for line in _git("rev-list", "--objects", "--branches", "--tags").splitlines():
        object_id, _, path = line.partition(" ")
        if not path or is_self_referential(path):
            continue
        if Path(path).suffix.lower() in SKIP_SUFFIXES:
            continue
        blobs.setdefault(object_id, path)
    return blobs


def scan_history(strict: bool) -> Tuple[List[str], int]:
    blobs = publishable_blobs()
    if not blobs:
        return [], 0
    request = "".join("{}\n".format(object_id) for object_id in blobs)
    result = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=str(REPO),
        input=request.encode(),
        capture_output=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise ScanError("git cat-file --batch failed")

    findings: List[str] = []
    scanned = 0
    stream = result.stdout
    offset = 0
    while offset < len(stream):
        newline = stream.find(b"\n", offset)
        if newline < 0:
            break
        header = stream[offset:newline].decode("ascii", "replace").split()
        offset = newline + 1
        if len(header) != 3:
            # "<oid> missing" — a malformed batch response, not a clean result.
            raise ScanError("unexpected cat-file header: {}".format(" ".join(header)))
        object_id, kind, size = header[0], header[1], int(header[2])
        payload, offset = stream[offset : offset + size], offset + size + 1
        if kind != "blob":
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            continue
        if b"\0" in payload:
            continue
        scanned += 1
        label = "{} ({})".format(blobs[object_id], object_id[:8])
        findings.extend(findings_in(text, label, strict))
    return findings, scanned


def scan_commit_metadata(strict: bool) -> List[str]:
    """Author and committer identity is published alongside the code."""
    findings = []
    rules = blocklist(strict)
    identities = _git(
        "log", "--branches", "--tags", "--format=%H%x1f%an <%ae>%x1f%cn <%ce>"
    )
    for line in identities.splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3:
            continue
        commit, author, committer = parts
        for role, identity in (("author", author), ("committer", committer)):
            redacted = without_maintainer(identity, strict)
            for rule in rules:
                if rule.pattern.search(redacted):
                    findings.append(
                        "commit {} {}: [{}] {}".format(
                            commit[:8], role, rule.name, identity
                        )
                    )
                    break
    return findings


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def _canary(worktree: bool) -> None:
    """Prove the scan can find something certainly present before trusting it."""
    if worktree:
        found = any(
            "clawflight" in path.read_text(encoding="utf-8", errors="replace")
            for path in worktree_files()
        )
    else:
        found = bool(publishable_blobs())
    if not found:
        raise ScanError(
            "the scan found nothing at all, so a clean result would be meaningless"
        )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--worktree", action="store_true", help="scan the working tree only"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="also flag the maintainer's own identity",
    )
    args = parser.parse_args(argv)

    scope = "working tree" if args.worktree else "all publishable history"
    rules = blocklist(args.strict)
    try:
        _canary(args.worktree)
        if args.worktree:
            findings, scanned = scan_worktree(args.strict)
            unit = "files"
        else:
            findings, scanned = scan_history(args.strict)
            findings += scan_commit_metadata(args.strict)
            unit = "blobs"
    except ScanError as error:
        print("SCAN FAILED: {}".format(error), file=sys.stderr)
        print("Treat this as a failure, not a pass.", file=sys.stderr)
        return 2

    print(
        "scanned {} {} in {} against {} rules".format(
            scanned, unit, scope, len(rules)
        )
    )
    if not findings:
        print("clean: no blocked pattern found")
        return 0

    print()
    for finding in findings[:60]:
        print("FAIL  {}".format(finding))
    if len(findings) > 60:
        print("... and {} more".format(len(findings) - 60))
    print(
        "\nIf the hits are in the working tree, edit the files and re-run.\n"
        "\n"
        "If they exist only in history, editing the working tree is NOT enough:\n"
        "the blobs stay reachable. Rewrite history, delete refs/original/*,\n"
        "expire the reflog, then force-push and discard every clone and fork.\n"
        "\n"
        "  git filter-branch -f --env-filter ... -- --branches --tags\n"
        "  git update-ref -d refs/original/refs/heads/main\n"
        "  git reflog expire --expire=now --all && git gc --prune=now\n"
        "\n"
        "Re-run this scan afterwards."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
