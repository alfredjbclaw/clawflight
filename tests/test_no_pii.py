"""The publication gate: no real personal data anywhere in this repository.

clawflight is the sanitized public extraction of a private family tracker whose
fixtures contained real third-party data. This test is the standing guarantee
that none of it ever arrives here — by a fresh paste, a helpful "realistic"
example, or a copied fixture.

It scans the **working tree**. Run ``tests/history_scan.sh`` to apply the same
blocklist to every blob in git history before publishing.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent

#: Directories that are never source and never publish.
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv", ".mypy_cache"}

#: Extensions we never scan as text.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".gz", ".pyc"}


def _blocklist():
    """(name, compiled pattern, why it is forbidden).

    Patterns match *shapes of real data*, not the words around them: a fixture
    may say "SkyMiles" but must never carry a membership number. This list is
    applied to the working tree AND to every blob in git history.
    """
    return [
        (
            "private-domains",
            re.compile(
                r"\b(?:ledecompany|livenation)\b|berchtold\.org", re.IGNORECASE
            ),
            "domains belonging to real people or employers",
        ),
        (
            "institutional-email",
            re.compile(r"@(?:cornell\.edu|gmail\.com|icloud\.com|outlook\.com|yahoo\.com)\b", re.IGNORECASE),
            "a real mailbox; fixtures must use example.com or a .example/.test domain",
        ),
        (
            "loyalty-number",
            re.compile(
                r"\b(?:skymiles|aadvantage|mileageplus|frequent\s*flyer)\b[^\n]{0,24}?\d{6,}",
                re.IGNORECASE,
            ),
            "a loyalty programme membership number",
        ),
        (
            "eticket-number",
            re.compile(r"\b(?:e-?ticket|ticket\s*(?:number|#))[^\n]{0,24}?\d{10,}", re.IGNORECASE),
            "an e-ticket number",
        ),
        (
            "payment-card",
            re.compile(
                r"\b(?:amex|american\s+express|visa|mastercard|card)\b[^\n]{0,24}?"
                r"(?:\*{2,}|x{4,}|ending\s+in\s*)\s*\d{4}\b",
                re.IGNORECASE,
            ),
            "a partial payment card number",
        ),
        (
            "us-phone-number",
            re.compile(r"(?<![\d.\-])(?:\+1[ .-]?)?\(?[2-9]\d{2}\)?[ .-][2-9]\d{2}[ .-]\d{4}(?![\d.\-])"),
            "a real-looking phone number; use +1555… only",
        ),
        (
            "home-coordinates",
            re.compile(r"\bhome_(?:lat|lon|latitude|longitude|address)\b", re.IGNORECASE),
            "a home location field; drive-home routing is out of scope",
        ),
    ]


def _scannable_files():
    for path in sorted(REPO.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(REPO).parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        if path.name in ("test_no_pii.py", "history_scan.sh"):
            # The gate and its standalone twin necessarily contain the patterns
            # they forbid.
            continue
        yield path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return ""


@pytest.mark.parametrize("name,pattern,why", _blocklist(), ids=lambda value: getattr(value, "pattern", value) if not hasattr(value, "pattern") else "re")
def test_no_real_personal_data_in_the_working_tree(name, pattern, why) -> None:
    hits = []
    for path in _scannable_files():
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            if pattern.search(line):
                hits.append(
                    "{}:{}: {}".format(path.relative_to(REPO), line_number, line.strip()[:160])
                )

    assert not hits, "{} ({}):\n{}".format(name, why, "\n".join(hits))


def test_every_fixture_address_uses_a_reserved_domain() -> None:
    # RFC 2606 reserves example.com/.example/.test/.invalid/.localhost for
    # documentation. Anything else in a fixture could be a real mailbox.
    address = re.compile(r"[\w.+-]+@([\w.-]+\.[A-Za-z]{2,})")
    reserved = re.compile(
        r"(?:^|\.)(?:example\.com|example\.net|example\.org|example|test|invalid|localhost)$",
        re.IGNORECASE,
    )
    offenders = []

    for path in sorted((REPO / "fixtures").rglob("*")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            for domain in address.findall(line):
                if not reserved.search(domain):
                    offenders.append(
                        "{}:{}: {}".format(path.relative_to(REPO), line_number, domain)
                    )

    assert not offenders, "non-reserved domains in fixtures:\n" + "\n".join(offenders)


def test_confirmation_codes_in_fixtures_are_obviously_synthetic() -> None:
    # Real PNRs are six opaque alphanumerics. Ours are always FAKE-prefixed so a
    # reader can tell at a glance that nothing here is a live booking.
    # The label is matched case-insensitively; the code itself is not, so prose
    # like "confirmation codes are synthetic" cannot look like a booking.
    labelled = re.compile(
        r"(?i:confirmation\s*(?:code|number)?|record\s+locator|booking\s+reference|conf#)"
        r"\s*[:#]?\s*\**\s*([A-Z0-9]{5,8})\b"
    )
    offenders = []

    for path in sorted((REPO / "fixtures").rglob("*")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            for code in labelled.findall(line):
                if not code.upper().startswith("FAKE"):
                    offenders.append(
                        "{}:{}: {}".format(path.relative_to(REPO), line_number, code)
                    )

    assert not offenders, "non-synthetic confirmation codes:\n" + "\n".join(offenders)


def test_no_owner_surname_in_the_working_tree() -> None:
    """Defence in depth beyond the third-party blocklist.

    The owner's own surname is not third-party PII, but a public repository has
    no reason to carry it either: its presence is a reliable signal that
    something was pasted from the private original rather than authored here.
    Scoped to the working tree — see ``tests/history_scan.sh`` for history.
    """
    pattern = re.compile(r"berchtold", re.IGNORECASE)
    hits = [
        "{}:{}".format(path.relative_to(REPO), line_number)
        for path in _scannable_files()
        for line_number, line in enumerate(_read(path).splitlines(), start=1)
        if pattern.search(line)
    ]

    assert hits == [], "owner surname found in:\n" + "\n".join(hits)


def test_no_module_imports_the_private_package() -> None:
    # The private engine's package name. Prose in EXTRACTION-PLAN.md may name
    # the source project; shipped code may never import from it.
    offenders = [
        str(path.relative_to(REPO))
        for path in (REPO / "clawflight").rglob("*.py")
        if "flight_tracker" in _read(path)
    ]

    assert offenders == []


def test_the_stripped_pii_audit_is_not_reintroduced() -> None:
    # The private project's PII audit catalogued the data by quoting it. It was
    # deliberately removed from this repository's history and must stay gone.
    assert not (REPO / "PII-AUDIT.md").exists()


def test_no_credential_value_is_committed() -> None:
    # Secrets are referenced by environment-variable NAME only.
    assignment = re.compile(
        r"(?i)\b(?:password|passwd|secret|api[_-]?key|token|bearer)\b\s*[:=]\s*"
        r"[\"']([^\"'\n]{8,})[\"']"
    )
    allowed = re.compile(r"(?i)_ENV$|^CLAWFLIGHT_|not-a-real-|expected-secret|gate-secret|^\$")
    offenders = []

    for path in _scannable_files():
        if path.suffix not in (".py", ".json", ".md", ".sh", ".toml", ".cfg"):
            continue
        for line_number, line in enumerate(_read(path).splitlines(), start=1):
            match = assignment.search(line)
            if match and not allowed.search(match.group(1)):
                offenders.append(
                    "{}:{}: {}".format(path.relative_to(REPO), line_number, line.strip()[:120])
                )

    assert not offenders, "possible committed credential:\n" + "\n".join(offenders)


def test_the_history_scan_script_is_present_and_executable() -> None:
    # The working-tree gate cannot see history; the script is how a maintainer
    # checks the rest before publishing.
    script = REPO / "tests" / "history_scan.sh"

    assert script.exists()
    assert script.stat().st_mode & 0o111, "history_scan.sh must be executable"


def _commits():
    """Every commit reachable from any ref, or a skip when git is unavailable."""
    try:
        revisions = subprocess.run(
            ["git", "rev-list", "--all"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - no git
        pytest.skip("git is not available")
    if revisions.returncode != 0 or not revisions.stdout.strip():
        pytest.skip("not a git repository with history")
    return revisions.stdout.split()


def test_git_commit_metadata_carries_no_personal_identity() -> None:
    """Author and committer identity is part of what a public repo publishes.

    ``git grep`` searches blob *contents* only, so a name or a personal address
    in commit metadata slips past every content scan. On a repository extracted
    for publication that is exactly the thing to catch: the default
    ``user.email`` on a personal machine is usually a real mailbox.
    """
    _commits()
    identities = subprocess.run(
        ["git", "log", "--all", "--format=%an <%ae>%n%cn <%ce>"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    offenders = []
    surname = re.compile(r"berchtold", re.IGNORECASE)
    for identity in sorted(set(identities.stdout.splitlines())):
        if not identity.strip():
            continue
        if surname.search(identity):
            offenders.append("{}  (personal name)".format(identity))
            continue
        for _name, pattern, _why in _blocklist():
            if pattern.search(identity):
                offenders.append("{}  (blocked pattern)".format(identity))
                break

    assert not offenders, (
        "personal identity in commit metadata:\n"
        + "\n".join(offenders)
        + "\n\nUse a GitHub noreply address, e.g.\n"
        "  git config user.email '<id>+<handle>@users.noreply.github.com'\n"
        "Existing commits need a history rewrite to change."
    )


def test_git_history_carries_no_blocked_pattern() -> None:
    """Apply the same blocklist to every blob reachable from any ref.

    Skipped when git is unavailable or this is not a repository, so the suite
    still runs from a source tarball.
    """
    commits = _commits()
    failures = []
    for name, pattern, why in _blocklist():
        found = subprocess.run(
            ["git", "grep", "-I", "-i", "-n", "-E", pattern.pattern] + commits,
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        # git grep exits 1 with no output when nothing matched.
        lines = [
            line
            for line in found.stdout.splitlines()
            if ":tests/test_no_pii.py:" not in line
        ]
        if lines:
            failures.append("{} ({}):\n{}".format(name, why, "\n".join(lines[:20])))

    assert not failures, "blocked patterns found in git history:\n" + "\n\n".join(failures)
